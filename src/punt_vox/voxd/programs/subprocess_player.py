"""Play a Part's file as a reduced-volume subprocess in the active directory.

Resolves the active Program's directory live from an injected ``PlayerDirectory``
on every spawn, builds the platform argv (``afplay``/``ffplay``) at reduced volume
so speech and chimes overlay it, and spawns it; the handle logs a non-zero exit.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import platform
import signal
from typing import TYPE_CHECKING, Self, final

from punt_vox.log_sanitize import SANITIZER

if TYPE_CHECKING:
    from pathlib import Path

    from punt_vox.voxd.programs.part import Part
    from punt_vox.voxd.programs.player_directory import PlayerDirectory

__all__ = ["SubprocessPlayer"]

logger = logging.getLogger(__name__)

_MUSIC_VOLUME = 30


@final
class SubprocessHandle:
    """A live player subprocess: awaited to completion, killable, exit-logged."""

    __slots__ = ("_proc",)
    _proc: asyncio.subprocess.Process

    def __new__(cls, proc: asyncio.subprocess.Process) -> Self:
        self = super().__new__(cls)
        self._proc = proc
        return self

    async def wait(self) -> int:
        """Block until the player exits, logging a non-zero exit code."""
        rc = await self._proc.wait()
        if rc != 0:
            await self._log_exit(rc)
        return rc

    async def kill(self) -> None:
        """Stop the player now and reap it.

        A natural exit can race the kill: the process may exit between the
        ``returncode`` check and ``kill()``, so ``kill()`` itself must be inside
        a suppress -- otherwise its ``ProcessLookupError`` would propagate
        through the loop's unguarded step and silently stop playback. The reap
        gets its *own* suppress so a raised ``kill()`` never skips it -- an
        already-exited process still has to be waited on to collect the zombie.
        """
        with contextlib.suppress(ProcessLookupError):
            if self._proc.returncode is None:
                self._proc.kill()
        with contextlib.suppress(ProcessLookupError):
            await self._proc.wait()

    def suspend(self) -> None:
        """Pause the player in place with ``SIGSTOP`` (transport pause).

        A stopped process never exits, so the loop's ``wait`` stays pending and
        the cursor never auto-advances while paused. A player that exited between
        spawn and pause is gone (``ProcessLookupError``): suppressed, since there
        is nothing to suspend.
        """
        self._send(signal.SIGSTOP)

    def resume(self) -> None:
        """Continue a ``SIGSTOP``-ed player with ``SIGCONT`` (transport resume)."""
        self._send(signal.SIGCONT)

    def terminate(self) -> None:
        """Kill the player now with ``SIGKILL`` (synchronous shutdown teardown).

        ``SIGKILL`` terminates even a ``SIGSTOP``-ed process, so a paused player is
        torn down on daemon stop with no orphan the OS could later ``SIGCONT`` into
        a stray burst. Fire-and-forget: no reap, since the daemon is exiting.
        """
        self._send(signal.SIGKILL)

    def _send(self, sig: signal.Signals) -> None:
        """Send ``sig`` to the player, suppressing an already-exited process."""
        with contextlib.suppress(ProcessLookupError):
            if self._proc.returncode is None:
                self._proc.send_signal(sig)

    async def _log_exit(self, rc: int) -> None:
        stderr_text = ""
        if self._proc.stderr is not None:
            stderr_bytes = await self._proc.stderr.read()
            # The player's stderr is free-form external text: a stray newline
            # would forge a second log record, a raw control byte would corrupt
            # a terminal on ``cat``. Escape it to a single auditable line.
            decoded = stderr_bytes.decode(errors="replace").strip()
            stderr_text = SANITIZER.escape(decoded)
        logger.warning("player exited with rc=%s: %s", rc, stderr_text)


@final
class SubprocessPlayer:
    """Play Parts from the active Program directory as reduced-volume subprocesses."""

    __slots__ = ("_directories",)
    _directories: PlayerDirectory

    def __new__(cls, directories: PlayerDirectory) -> Self:
        self = super().__new__(cls)
        self._directories = directories
        return self

    async def play(self, part: Part) -> SubprocessHandle:
        """Spawn the player for ``part`` at its active-source path, resolved now."""
        proc = await asyncio.create_subprocess_exec(
            *self._command(self._directories.locate(part)),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        return SubprocessHandle(proc)

    @staticmethod
    def _command(path: Path) -> list[str]:
        """Return the reduced-volume player argv for ``path``."""
        if platform.system() == "Darwin":
            return ["afplay", "--volume", "0.3", str(path)]
        return [
            "ffplay",
            "-nodisp",
            "-autoexit",
            "-volume",
            str(_MUSIC_VOLUME),
            str(path),
        ]
