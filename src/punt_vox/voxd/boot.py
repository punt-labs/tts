"""voxd's boot sequence: crash sinks, then logging, then keys, then the daemon.

Split from the daemon module because the ORDER here is the whole content. The
daemon class composes subsystems; this composes the *startup*, where each step
exists to make the next one's failures visible. Keeping it beside the class it
builds hid that: the sequence read as CLI plumbing rather than as the load-
bearing thing it is.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Self, final

from punt_vox.logging_config import configure_daemon_logging
from punt_vox.paths import ensure_user_dirs
from punt_vox.voxd.config import DaemonConfig
from punt_vox.voxd.crash_logging import CrashLogger
from punt_vox.voxd.daemon import VoxDaemon
from punt_vox.voxd.handler_registry import HandlerRegistry
from punt_vox.voxd.health import DaemonHealth
from punt_vox.voxd.playback import PlaybackQueue
from punt_vox.voxd.router import WebSocketRouter
from punt_vox.voxd.synthesis import SynthesisPipeline

logger = logging.getLogger(__name__)

__all__ = ["DaemonBoot"]


@final
class DaemonBoot:
    """Bring voxd up in the one order that keeps every failure visible."""

    __slots__ = ("_host", "_port")
    _host: str
    _port: int

    def __new__(cls, host: str, port: int) -> Self:
        self = super().__new__(cls)
        self._host = host
        self._port = port
        return self

    def run(self) -> None:
        """Boot the daemon and serve until shutdown."""
        config = self._prepare_logging_and_config()
        daemon = self._compose(config)
        logger.info("Starting voxd on %s:%d", self._host, self._port)
        asyncio.run(daemon.run(self._host, self._port))

    @staticmethod
    def _prepare_logging_and_config() -> DaemonConfig:
        """Install crash sinks, configure logging, load keys; answer the config.

        The bootstrap excepthook goes in FIRST, before anything can raise and
        before the file handler exists: it writes with raw os syscalls, so a
        crash inside ``ensure_user_dirs`` or ``configure_daemon_logging`` itself
        lands in ``vox-boot.log`` instead of vanishing -- the daemon has no
        stderr to fall back on.
        """
        crash = CrashLogger(logger)
        boot_log = DaemonConfig.user_log_dir() / "vox-boot.log"
        crash.install_bootstrap_excepthook(boot_log)

        ensure_user_dirs()
        config = DaemonConfig.for_user()

        configure_daemon_logging()
        # The file handler is live now: upgrade from the bootstrap sink so the
        # rest of startup, and any asyncio.run crash, lands in vox.log beside
        # everything else.
        crash.install_excepthook()
        config.log_environment()

        loaded = config.load_keys()
        if loaded:
            logger.info(
                "Loaded provider keys from %s: %s", config.config_dir, sorted(loaded)
            )
        else:
            logger.info("No provider keys loaded from %s", config.config_dir)
        return config

    def _compose(self, config: DaemonConfig) -> VoxDaemon:
        """Wire the subsystems and answer the daemon that owns them."""
        auth_token = config.read_or_create_token()
        programs = VoxDaemon.build_programs()
        playback = PlaybackQueue()
        synthesis = SynthesisPipeline(playback_mutex=playback.mutex)

        # Health needs the router's client_count and the router needs health, so
        # the count is read through a lambda that resolves after both exist.
        health = DaemonHealth(playback, lambda: router.client_count, self._port)
        router = WebSocketRouter(
            handlers=HandlerRegistry(
                synthesis=synthesis,
                playback=playback,
                programs=programs,
                health=health,
            ).build(),
            auth_token=auth_token,
        )
        return VoxDaemon(
            config=config,
            playback=playback,
            synthesis=synthesis,
            programs=programs,
            health=health,
            router=router,
        )
