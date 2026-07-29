"""VoxDaemon -- composition root for the voxd audio server."""
# pyright: reportPrivateUsage=false
# Internal module within the voxd package -- cross-module private access is expected.

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from socket import socket
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from starlette.requests import Request

import typer
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute

from punt_vox.logging_config import configure_daemon_logging
from punt_vox.paths import ensure_user_dirs
from punt_vox.providers.elevenlabs_music import ElevenLabsMusicProvider
from punt_vox.voxd.config import (
    DaemonConfig,
    _config_dir,
    _install_token_redact_filter,
    _log_dir,
    _run_dir,
)
from punt_vox.voxd.crash_logging import CrashLogger
from punt_vox.voxd.handler_registry import HandlerRegistry
from punt_vox.voxd.health import DaemonHealth
from punt_vox.voxd.music_player import MusicPlayerSubsystem
from punt_vox.voxd.playback import PlaybackQueue
from punt_vox.voxd.programs.music_producer import LengthPolicy, MusicProducer
from punt_vox.voxd.programs.wiring import ProgramSubsystem
from punt_vox.voxd.router import WebSocketRouter
from punt_vox.voxd.synthesis import SynthesisPipeline

logger = logging.getLogger(__name__)

# Inbound WebSocket frame cap (uvicorn defaults to 16 MiB). 256 KiB comfortably
# fits the largest legitimate client->daemon frame (a music request) while
# denying multi-MB frames that would burden the daemon's event loop.
_WS_MAX_FRAME_BYTES = 256 * 1024

DEFAULT_PORT = 8421
DEFAULT_HOST = "127.0.0.1"

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "VoxDaemon",
    "build_app",
    "cli",
    "entrypoint",
]


class VoxDaemon:
    """Composition root that wires all daemon subsystems and runs the server."""

    __slots__ = (
        "_config",
        "_health",
        "_playback",
        "_programs",
        "_router",
        "_synthesis",
    )

    _config: DaemonConfig
    _health: DaemonHealth
    _playback: PlaybackQueue
    _programs: ProgramSubsystem
    _router: WebSocketRouter
    _synthesis: SynthesisPipeline

    def __new__(
        cls,
        config: DaemonConfig,
        playback: PlaybackQueue,
        synthesis: SynthesisPipeline,
        programs: ProgramSubsystem,
        health: DaemonHealth,
        router: WebSocketRouter,
    ) -> Self:
        self = super().__new__(cls)
        self._config = config
        self._playback = playback
        self._synthesis = synthesis
        self._programs = programs
        self._health = health
        self._router = router
        return self

    def build_app(self) -> Starlette:
        """Build the Starlette ASGI application with lifespan management."""
        return VoxDaemon._starlette(
            health=self._health,
            router=self._router,
            lifespan=self._lifespan,
        )

    async def run(self, host: str, port: int) -> None:
        """Start uvicorn and serve until shutdown."""
        # Route fire-and-forget task exceptions to vox.log -- with no stderr
        # handler, the loop's default printer would otherwise write to nowhere.
        CrashLogger(logger).install_loop_handler(asyncio.get_running_loop())
        app = self.build_app()

        if host not in ("127.0.0.1", "::1", "localhost"):
            logger.warning(
                "Binding to %s -- voxd is accessible from the network. "
                "Ensure VOXD_TOKEN is set on all clients.",
                host,
            )

        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_config=None,
            log_level="warning",
            access_log=True,
            # Cap inbound WS frames well below uvicorn's 16 MiB default: the
            # largest legitimate client->daemon frame is a music request (a base
            # prompt + 12 variations, ~tens of KiB). A tighter bound stops a
            # token-bearing client from streaming multi-MB frames at the event loop.
            ws_max_size=_WS_MAX_FRAME_BYTES,
        )
        _install_token_redact_filter()
        server = uvicorn.Server(config)

        original_startup = server.startup
        daemon_cfg = self._config

        async def _startup_with_port_file(
            sockets: list[socket] | None = None,
        ) -> None:
            await original_startup(sockets=sockets)
            if server.servers and server.servers[0].sockets:
                actual_port = server.servers[0].sockets[0].getsockname()[1]
                daemon_cfg.write_port_file(actual_port)
                logger.info("voxd listening on http://%s:%d", host, actual_port)
            else:
                logger.error("Server started but no bound sockets; shutting down")
                server.should_exit = True

        server.startup = _startup_with_port_file  # type: ignore[method-assign]
        await server.serve()

    @asynccontextmanager
    async def _lifespan(self, _app: Starlette) -> AsyncGenerator[None]:
        """Run the playback consumer, the control writer, and the playback loop.

        The Programs subsystem contributes two background tasks: the single
        control-channel writer (the sole mutator of the Program, O2) and the
        playback loop that plays and advances Parts. The music player adds a
        third -- the lux scene publisher, which pushes the initial ``vox.music``
        scene and re-pushes on every change signal. All ride the daemon's
        lifetime and are cancelled on shutdown; a down luxd never blocks them.
        """
        service = self._programs.service
        music = MusicPlayerSubsystem(service, service.changes)
        consumer_task = asyncio.create_task(self._playback.consumer())
        control_task = asyncio.create_task(service.serve_control())
        playback_task = asyncio.create_task(service.run_playback())
        scene_task = asyncio.create_task(music.run())
        logger.info("Playback consumer, control writer, loop, and scene publisher up")
        try:
            yield
        finally:
            await VoxDaemon._cancel(scene_task)
            await VoxDaemon._cancel(playback_task)
            await VoxDaemon._cancel(control_task)
            service.shutdown()
            await VoxDaemon._cancel(consumer_task)
            with contextlib.suppress(Exception):
                self._config.remove_port_file()
            logger.info("voxd stopped")

    @staticmethod
    async def _cancel(task: asyncio.Task[None]) -> None:
        """Cancel a background task and await its exit, swallowing the teardown.

        ``CancelledError`` is a ``BaseException`` (not ``Exception``) since 3.8, so
        it must be suppressed explicitly -- otherwise the *expected* cancel of the
        first task would propagate out of ``_lifespan``'s finally and skip every
        remaining teardown step (leaking the writer, the fill, and the port file).
        """
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    @staticmethod
    def read_port_file() -> int | None:
        """Read the daemon port from the port file."""
        return DaemonConfig.read_port_file(_run_dir())

    @staticmethod
    def read_token_file() -> str | None:
        """Read the daemon auth token."""
        return DaemonConfig.read_token_file(_run_dir())

    @staticmethod
    def _programs_root() -> Path:
        """Return the root directory under which saved Programs live.

        Each Program is a pool directory directly under the music root
        (``~/Music/vox/<name>/``) so desktop music players scan the pools with
        no ``programs/`` segment in the way.
        """
        from punt_vox.dirs import default_output_dir

        return default_output_dir()

    @staticmethod
    def _build_programs() -> ProgramSubsystem:
        """Build the Programs subsystem with the production ElevenLabs producer.

        The producer is injected (not hard-wired in ``ProgramSubsystem``) so tests
        drive the subsystem with a fake; the ElevenLabs default is applied here, at
        the daemon composition root.
        """
        producer = MusicProducer(ElevenLabsMusicProvider(), LengthPolicy())
        return ProgramSubsystem(VoxDaemon._programs_root(), producer)

    @staticmethod
    def _health_handler(
        health: DaemonHealth,
    ) -> Callable[[Request], object]:
        """Return an async handler that serves the minimal health payload."""

        async def _handler(_request: Request) -> JSONResponse:
            return JSONResponse(health.minimal_payload())

        return _handler

    @staticmethod
    def _starlette(
        *,
        health: DaemonHealth,
        router: WebSocketRouter,
        lifespan: (
            Callable[[Starlette], AbstractAsyncContextManager[None]] | None
        ) = None,
    ) -> Starlette:
        """Build a Starlette app from pre-wired components."""
        routes: list[Route | WebSocketRoute] = [
            Route(
                "/health",
                VoxDaemon._health_handler(health),
                methods=["GET"],
            ),
            WebSocketRoute("/ws", router.handle_connection),
        ]
        return Starlette(routes=routes, lifespan=lifespan)

    @staticmethod
    def entrypoint() -> None:
        """Console script entry point -- invokes the typer CLI."""
        cli()

    @staticmethod
    def create_app(
        *,
        playback: PlaybackQueue | None = None,
        programs: ProgramSubsystem | None = None,
        health: DaemonHealth | None = None,
        synthesis: SynthesisPipeline | None = None,
        router: WebSocketRouter | None = None,
        auth_token: str | None = None,
    ) -> Starlette:
        """Build the Starlette ASGI app for tests.

        Accepts pre-constructed subsystems. Creates defaults for anything
        not provided, wiring them together as the real daemon would.
        """
        pb = playback or PlaybackQueue()
        syn = synthesis or SynthesisPipeline(playback_mutex=pb.mutex)
        progs = programs or VoxDaemon._build_programs()
        hlth = health or DaemonHealth(pb, lambda: 0, 0)

        if router is None:
            handlers = HandlerRegistry(
                synthesis=syn,
                playback=pb,
                programs=progs,
                health=hlth,
            ).build()
            router = WebSocketRouter(
                handlers=handlers,
                auth_token=auth_token,
            )

        return VoxDaemon._starlette(health=hlth, router=router)


# Public module API: the voxd console-script entrypoint and app factory.
build_app = VoxDaemon.create_app
entrypoint = VoxDaemon.entrypoint


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

cli = typer.Typer(add_completion=False)


@cli.callback(invoke_without_command=True)
def main(
    port: int = typer.Option(DEFAULT_PORT, "--port", "-p", help="Listen port"),
    host: str = typer.Option(
        DEFAULT_HOST, "--host", envvar="VOXD_BIND", help="Listen host"
    ),
) -> None:
    """Start the voxd audio server daemon."""
    crash = CrashLogger(logger)
    # FIRST, before anything can raise and before the file handler exists: an
    # emergency sink that writes with raw os syscalls, so a crash in
    # ``ensure_user_dirs`` or ``configure_daemon_logging`` itself lands in
    # ``vox-boot.log`` instead of vanishing (the daemon has no stderr).
    crash.install_bootstrap_excepthook(_log_dir() / "vox-boot.log")

    ensure_user_dirs()

    daemon_cfg = DaemonConfig(
        run_dir=_run_dir(), config_dir=_config_dir(), log_dir=_log_dir()
    )

    configure_daemon_logging()
    # The file handler is now live: upgrade from the bootstrap sink to the
    # file-backed logger so the rest of startup and ``asyncio.run`` crashes land
    # in ``vox.log`` beside everything else.
    crash.install_excepthook()
    daemon_cfg.log_environment()

    loaded_keys = daemon_cfg.load_keys()
    if loaded_keys:
        logger.info(
            "Loaded provider keys from %s: %s",
            daemon_cfg.config_dir,
            sorted(loaded_keys),
        )
    else:
        logger.info("No provider keys loaded from %s", daemon_cfg.config_dir)

    auth_token = daemon_cfg.read_or_create_token()

    programs = VoxDaemon._build_programs()
    playback = PlaybackQueue()
    synthesis = SynthesisPipeline(playback_mutex=playback.mutex)

    # Health needs the router's client_count, but router needs health.
    # Use a lambda to defer the lookup.
    health = DaemonHealth(playback, lambda: ws_router.client_count, port)

    handlers = HandlerRegistry(
        synthesis=synthesis,
        playback=playback,
        programs=programs,
        health=health,
    ).build()
    ws_router = WebSocketRouter(
        handlers=handlers,
        auth_token=auth_token,
    )

    daemon = VoxDaemon(
        config=daemon_cfg,
        playback=playback,
        synthesis=synthesis,
        programs=programs,
        health=health,
        router=ws_router,
    )

    logger.info("Starting voxd on %s:%d", host, port)
    asyncio.run(daemon.run(host, port))


if __name__ == "__main__":
    cli()
