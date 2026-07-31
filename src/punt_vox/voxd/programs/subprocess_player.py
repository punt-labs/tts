"""Play a Part's file as a reduced-volume subprocess in the active directory.

Resolves the active Program's directory live from an injected ``PlayerDirectory``
on every spawn, builds the ``ffplay`` argv at reduced volume so speech and chimes
overlay it -- optionally seeked to a resume offset -- and spawns it; the handle
logs a non-zero exit.

The player is ``ffplay`` on both macOS and Linux (``ffmpeg``/``ffplay`` are already
vox dependencies). macOS's built-in ``afplay`` cannot seek, and a seek is what a
click-free pause/resume needs: pause tears the player down and resume re-spawns it
at ``-ss <offset>``. Unifying on ``ffplay`` gives one seek-capable code path on both
platforms rather than a special-cased ``afplay`` that would have to pre-trim a temp
segment to fake a seek.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
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

    def stop_gracefully(self) -> None:
        """Ask the player to exit cleanly with ``SIGTERM`` (transport pause).

        ``ffplay`` handles ``SIGTERM`` by closing its audio device and exiting, so
        the device stops with no underrun click -- the whole point of tearing the
        player down on pause instead of freezing it. The loop reaps the exit
        through its usual ``wait``; a player that exited between spawn and pause is
        gone (``ProcessLookupError``): suppressed, since there is nothing to stop.
        """
        self._send(signal.SIGTERM)

    def terminate(self) -> None:
        """Kill the player now with ``SIGKILL`` (synchronous shutdown teardown).

        Fire-and-forget: no reap, since the daemon is exiting. A paused source has
        already torn its player down, so this covers only a player caught
        mid-spawn at shutdown.
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

    async def play(self, part: Part, offset: float = 0.0) -> SubprocessHandle:
        """Spawn the player for ``part`` at its path, seeked to ``offset`` seconds."""
        proc = await asyncio.create_subprocess_exec(
            *self._command(self._directories.locate(part), offset),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        return SubprocessHandle(proc)

    @staticmethod
    def _command(path: Path, offset: float) -> list[str]:
        """Return the reduced-volume ``ffplay`` argv for ``path``.

        A positive ``offset`` adds ``-ss <seconds>`` so a resumed part starts where
        pause froze it; a fresh part (offset 0) plays from the top.
        """
        argv = ["ffplay", "-nodisp", "-autoexit", "-volume", str(_MUSIC_VOLUME)]
        if offset > 0:
            argv += ["-ss", f"{offset:.3f}"]
        argv.append(str(path))
        return argv
