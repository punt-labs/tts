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

    from punt_vox.voxd.music_player.hub_ports import LuxClientFactory

import typer
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute

from punt_vox.providers.elevenlabs_music import ElevenLabsMusicProvider
from punt_vox.voxd.background_tasks import BackgroundTasks
from punt_vox.voxd.config import (
    DaemonConfig,
    _install_token_redact_filter,
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
        "_lux_clients",
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
    _lux_clients: LuxClientFactory | None

    def __new__(
        cls,
        config: DaemonConfig,
        playback: PlaybackQueue,
        synthesis: SynthesisPipeline,
        programs: ProgramSubsystem,
        health: DaemonHealth,
        router: WebSocketRouter,
        # None means voxd's real app-identity clients, resolved against the
        # running luxd. It is a constructor argument rather than a lookup inside
        # ``_lifespan`` so that whoever composes the daemon decides which luxd it
        # reaches: a test that drives the real lifespan to exercise task wiring
        # would otherwise connect to the operator's live hub and publish a
        # ``vox.music`` scene from its own scratch store, putting a second
        # "Music" frame on their display beside the real one.
        lux_clients: LuxClientFactory | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._config = config
        self._playback = playback
        self._synthesis = synthesis
        self._programs = programs
        self._health = health
        self._router = router
        self._lux_clients = lux_clients
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
        music = MusicPlayerSubsystem(service, service.changes, self._lux_clients)
        # Two groups because ``service.shutdown()`` has to land BETWEEN them: the
        # producers stop, then the service drains, then the consumer that drains
        # it stops. Collapsing both into one group would cancel the consumer
        # before ``shutdown()`` and drop whatever draining enqueues.
        producers = BackgroundTasks()
        consumer = BackgroundTasks()
        consumer.start(self._playback.consumer)
        producers.start(service.serve_control)
        # The mpv supervisor spawns the one program-tier process and keeps it up;
        # the loop waits on it before any load. Its cancellation on shutdown drives
        # the graceful mpv teardown (quit then hard-kill).
        producers.start(service.run_player_supervisor)
        producers.start(service.run_playback)
        producers.start(music.run)
        logger.info("Playback consumer, control writer, loop, mpv, and scene up")
        try:
            yield
        finally:
            # Reverse start order: scene, playback, supervisor, then the writer.
            await producers.stop_all()
            service.shutdown()
            await consumer.stop_all()
            with contextlib.suppress(Exception):
                self._config.remove_port_file()
            logger.info("voxd stopped")

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
    def build_programs() -> ProgramSubsystem:
        """Build the Programs subsystem with the production ElevenLabs producer.

        The producer is injected (not hard-wired in ``ProgramSubsystem``) so tests
        drive the subsystem with a fake; the ElevenLabs default is applied here, at
        the daemon composition root.
        """
        producer = MusicProducer(ElevenLabsMusicProvider(), LengthPolicy())
        mpv_socket = _run_dir() / "mpv.sock"
        return ProgramSubsystem(VoxDaemon._programs_root(), producer, mpv_socket)

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
        progs = programs or VoxDaemon.build_programs()
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
    # Imported here, not at module scope: boot imports this module for the
    # class it composes, so a top-level import back would be a cycle.
    from punt_vox.voxd.boot import DaemonBoot

    DaemonBoot(host, port).run()


if __name__ == "__main__":
    cli()
