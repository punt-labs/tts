"""``MpvLauncher`` -- spawn the one mpv and connect its JSON-IPC socket.

The launcher owns the *mechanics* of bring-up and nothing about the lifecycle:
it builds the hardened spawn argv, starts the audio-only process, and connects
the IPC socket (retrying the brief window before mpv opens it). The supervisor
decides *when* to launch and what to do on failure; the launcher only knows
*how*. Keeping the two apart lets the lifecycle machine
(``docs/mpv-program-player.tex``) read as pure state transitions, with the OS
and socket detail confined here.

The reduced program volume lives here too: the tier plays under speech and
chimes, so mpv starts at ``_MUSIC_VOLUME`` -- the music half of the two-tier
static rebalance.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd.programs.mpv.mpv_client import MpvClient

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from punt_vox.voxd.programs.sleeper import Sleeper

__all__ = ["MpvLauncher"]

logger = logging.getLogger(__name__)

_MPV_BINARY = "mpv"
_MUSIC_VOLUME = 60  # the reduced program volume so speech and chimes overlay music
_CONNECT_ATTEMPTS = 50
_CONNECT_DELAY = 0.1


@final
class MpvLauncher:
    """Spawn mpv and connect its IPC socket -- the bring-up mechanics (I2/I5)."""

    __slots__ = ("_sleeper", "_socket")
    _socket: Path
    _sleeper: Sleeper

    def __new__(cls, socket: Path, sleeper: Sleeper) -> Self:
        self = super().__new__(cls)
        self._socket = socket
        self._sleeper = sleeper
        return self

    async def spawn(self) -> asyncio.subprocess.Process | None:
        """Start the mpv process, or ``None`` if the binary is missing/too old."""
        try:
            return await asyncio.create_subprocess_exec(
                _MPV_BINARY,
                *self._flags(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            logger.error("mpv spawn failed: %s", exc)
            return None

    async def connect(self, on_eof: Callable[[], None]) -> MpvClient | None:
        """Connect the IPC socket, retrying the brief window before mpv opens it."""
        for _ in range(_CONNECT_ATTEMPTS):
            try:
                reader, writer = await asyncio.open_unix_connection(str(self._socket))
            except (FileNotFoundError, ConnectionRefusedError, OSError):
                await self._sleeper.sleep(_CONNECT_DELAY)
                continue
            client = MpvClient(reader, writer, on_eof)
            client.start()
            return client
        logger.error("mpv connect failed after %d attempts", _CONNECT_ATTEMPTS)
        return None

    def _flags(self) -> list[str]:
        """Return the mpv flags -- audio-only, idle, IPC, no network/scripts, quiet."""
        return [
            "--idle=yes",
            "--no-video",
            "--vo=null",
            f"--input-ipc-server={self._socket}",
            "--no-config",
            # A crafted media path must not reach the network or run a script:
            # --ytdl=no kills the youtube-dl/yt-dlp URL hook, --load-scripts=no
            # blocks user-script execution (--no-config already bars the config
            # dir). loadfile then plays only the contained local file it is given.
            "--ytdl=no",
            "--load-scripts=no",
            f"--volume={_MUSIC_VOLUME}",
            "--gapless-audio=yes",
            "--terminal=no",
            "--msg-level=all=warn",
        ]
