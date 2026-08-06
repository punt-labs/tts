"""``MpvSupervisor`` -- spawn, connect, crash-detect, and restart the one mpv.

The supervisor owns the mpv *process and connection* lifecycle and nothing about
what plays: it spawns mpv, connects the IPC socket, detects a crash (the client's
reader signals socket EOF), and restarts with a bounded backoff and a cap. It
*never* issues ``loadfile`` -- that is the loop's alone (single-loadfile-owner).

The lifecycle is the six-mode machine of ``docs/mpv-program-player.tex``:
``down -> starting -> ready``; a crash or a failed bring-up routes through
``crashed -> restarting`` and, at the cap, terminates in ``failed`` (the standing
``PLAYER_UNAVAILABLE``/``PLAYER_FAILED`` fault the client sees). A successful
``Connect`` clears the restart counter, so the cap counts only *consecutive*
attempts that never reached ``ready`` -- a cold start that never connects and a
crash loop that never reconnects both terminate, while a crash that recovers does
not accumulate toward the cap (I4).

The minimum mpv version is pinned here: the IPC command names and the
``end-file`` reason values are the contract this design rests on, and ``doctor``
imports :data:`MPV_MIN_VERSION` to gate an installed mpv against it. The spawn
argv and socket-connect mechanics live in :class:`MpvLauncher`; this module is
the lifecycle machine that decides *when* to launch.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Self, final

from punt_vox.types_programs.mpv_event import MpvCommand
from punt_vox.types_programs.playback_fault import PlaybackFault, PlaybackFaultKind
from punt_vox.voxd.programs.mpv.mpv_launcher import MpvLauncher
from punt_vox.voxd.programs.mpv.orphan_reaper import (
    OrphanReaper,
    OrphanUnreachableError,
)

if TYPE_CHECKING:
    from pathlib import Path

    from punt_vox.voxd.programs.mpv.mpv_client import MpvClient
    from punt_vox.voxd.programs.sleeper import Sleeper

__all__ = ["MPV_MIN_VERSION", "MpvState", "MpvSupervisor"]

logger = logging.getLogger(__name__)

MPV_MIN_VERSION: tuple[int, int, int] = (0, 35, 0)
"""The lowest mpv whose JSON-IPC command set and ``end-file`` reasons this
player relies on. ``doctor`` imports this and fails a too-old mpv."""

_SPAWN_BACKOFF_SECONDS = 2.0
_MAX_RESTARTS = 3


class MpvState(StrEnum):
    """The mode of the one mpv process over its lifecycle (I3 fault gate)."""

    DOWN = "down"  # no process -- before the first spawn, after a clean shutdown
    STARTING = "starting"  # spawned, socket not yet connected (clean first bring-up)
    READY = "ready"  # alive and connected -- the only mode commands may be issued
    CRASHED = "crashed"  # process/socket died, or a bring-up failed; a restart is owed
    RESTARTING = "restarting"  # respawning within the cap, carrying the crash fault
    FAILED = "failed"  # the restart cap was exceeded; a standing hard fault stands


@final
class MpvSupervisor:
    """Own the mpv process/connection lifecycle; never issue ``loadfile`` (I2/I5)."""

    __slots__ = (
        "_client",
        "_crashed",
        "_ever_ready",
        "_fault",
        "_launcher",
        "_proc",
        "_ready",
        "_reaped",
        "_reaper",
        "_restarts",
        "_sleeper",
        "_state",
    )
    _sleeper: Sleeper
    _launcher: MpvLauncher
    _ready: asyncio.Event
    _crashed: asyncio.Event
    _client: MpvClient | None
    _proc: asyncio.subprocess.Process | None
    _fault: PlaybackFault | None
    _state: MpvState
    _restarts: int
    _ever_ready: bool
    _reaper: OrphanReaper
    _reaped: bool

    def __new__(cls, socket: Path, sleeper: Sleeper) -> Self:
        self = super().__new__(cls)
        self._sleeper = sleeper
        self._launcher = MpvLauncher(socket, sleeper)
        self._ready = asyncio.Event()
        self._crashed = asyncio.Event()
        self._client = None
        self._proc = None
        self._fault = None
        self._state = MpvState.DOWN
        self._restarts = 0
        self._ever_ready = False
        self._reaper = OrphanReaper(socket)
        self._reaped = False
        return self

    @property
    def state(self) -> MpvState:
        """Return the current process mode (the client-observable lifecycle)."""
        return self._state

    @property
    def fault(self) -> PlaybackFault | None:
        """Return the standing process-level fault, or ``None`` when healthy (I3)."""
        return self._fault

    @property
    def is_ready(self) -> bool:
        """Return whether mpv is connected and commands may be issued (I1)."""
        return (
            self._state is MpvState.READY
            and self._client is not None
            and self._client.is_ready
        )

    def current_client(self) -> MpvClient | None:
        """Return the live connection, or ``None`` when not ``ready``."""
        return self._client if self.is_ready else None

    async def wait_ready(self) -> None:
        """Block until mpv is ready -- the loop's ``WaitReady`` step (I1)."""
        await self._ready.wait()

    async def run(self) -> None:
        """Supervise mpv for the daemon's lifetime; tear it down on exit.

        Shutdown is a task cancellation: it interrupts whichever ``await`` the
        loop is parked on and runs the graceful teardown in the ``finally``.
        """
        try:
            await self._supervise()
        finally:
            await self._teardown()

    async def _supervise(self) -> None:
        """Supervise mpv, standing a fault on an unexpected error.

        The restart loop handles a modeled crash. An UNEXPECTED error -- a
        supervisor bug -- is logged and stood as a hard ``PLAYER_FAILED`` fault,
        then parked, so ``status`` reports it rather than the daemon hanging on
        ``wait_ready`` with the task dead and no fault set.
        """
        try:
            while True:
                if await self._bring_up():
                    await self._crashed.wait()
                    await self._note_crash()
                if not await self._restart_or_fail():
                    await self._park_failed()  # blocks until shutdown cancels run
        except Exception:
            logger.exception("mpv supervisor: unexpected error; standing failed")
            self._stand_failed(
                "mpv supervisor failed unexpectedly", PlaybackFaultKind.PLAYER_FAILED
            )
            await self._park_failed()

    async def _park_failed(self) -> None:
        """Hold the standing ``failed`` fault until the daemon cancels the task.

        The program tier is dead but the daemon (and the notification tier) stays
        up. Parking -- rather than returning into teardown -- keeps ``state ==
        failed`` observable until shutdown cancels ``run`` and drives the clean
        ``down`` teardown.
        """
        await asyncio.Event().wait()

    async def _bring_up(self) -> bool:
        """Spawn and connect mpv; on success reach ``ready`` and clear the counter."""
        if not self._reap_orphan():
            self._record_bring_up_failure()
            return False
        self._crashed.clear()
        self._state = MpvState.RESTARTING if self._fault else MpvState.STARTING
        proc = await self._launcher.spawn()
        if proc is None:
            self._record_bring_up_failure()
            return False
        self._proc = proc
        client = await self._launcher.connect(self._on_reader_eof)
        if client is None:
            self._discard_proc()
            self._record_bring_up_failure()
            return False
        self._reach_ready(client)
        return True

    def _reap_orphan(self) -> bool:
        """Reap a prior-daemon orphan once at cold start; report bring-up may proceed.

        The reap runs before the first spawn only (I2 startup hygiene) -- once we
        own the socket, re-probing would quit our *own* mpv. A denied (EACCES)
        probe raises :class:`OrphanUnreachableError`: a live mpv may still own the
        socket, so bring-up aborts and folds into the restart-to-failed path
        rather than spawning a second mpv on a fresh inode.
        """
        if self._reaped:
            return True
        try:
            self._reaper.reap()
        except OrphanUnreachableError:
            return False
        self._reaped = True
        return True

    def _reach_ready(self, client: MpvClient) -> None:
        """Enter ``ready``: healthy, counter cleared, the loop's gate opened."""
        self._client = client
        self._state = MpvState.READY
        self._ever_ready = True
        self._restarts = 0
        self._fault = None
        self._ready.set()

    def _record_bring_up_failure(self) -> None:
        """Route a failed spawn/connect to ``crashed`` with a standing fault."""
        self._ready.clear()
        self._state = MpvState.CRASHED
        self._fault = PlaybackFault.process_level(
            "mpv could not be started or connected",
            PlaybackFaultKind.PLAYER_UNAVAILABLE,
        )

    async def _note_crash(self) -> None:
        """Handle a detected crash: close the dead client, discard the process, fault.

        The client's reader has already failed every pending command future and
        resolved the loop's ended-future with ``crashed`` (I7); the supervisor
        closes the dead connection so its ``StreamWriter`` is not leaked to GC,
        tears the dead process down, and records the standing crash fault.
        """
        self._ready.clear()
        await self._close_dead_client()
        self._discard_proc()
        self._state = MpvState.CRASHED
        self._fault = PlaybackFault.process_level(
            "mpv crashed; restarting", PlaybackFaultKind.PLAYER_CRASH
        )

    async def _close_dead_client(self) -> None:
        """Close the crashed connection, ignoring errors from its broken transport."""
        client = self._client
        self._client = None
        if client is None:
            return
        with contextlib.suppress(Exception):
            await client.close()

    async def _restart_or_fail(self) -> bool:
        """Restart within the cap (return ``True``), else give up (``False``, I4)."""
        if self._restarts >= _MAX_RESTARTS:
            self._enter_failed()
            return False
        self._restarts += 1
        await self._sleeper.sleep(_SPAWN_BACKOFF_SECONDS)
        return True

    def _enter_failed(self) -> None:
        """Terminate at ``failed`` -- the cap is exceeded; the program tier is dead."""
        kind = (
            PlaybackFaultKind.PLAYER_FAILED
            if self._ever_ready
            else PlaybackFaultKind.PLAYER_UNAVAILABLE
        )
        self._stand_failed("mpv could not be kept running", kind)

    def _stand_failed(self, reason: str, kind: PlaybackFaultKind) -> None:
        """Clear readiness and stand a hard process-level fault at ``failed``."""
        self._ready.clear()
        self._state = MpvState.FAILED
        self._fault = PlaybackFault.process_level(reason, kind)

    def _on_reader_eof(self) -> None:
        """Wake the supervise loop on socket EOF (the client's crash callback)."""
        self._crashed.set()

    async def _teardown(self) -> None:
        """Quit mpv gracefully then hard-kill the process (graceful, then fallback).

        The standing fault is cleared as the process enters ``down`` so I3
        (strict) holds through shutdown: a ``status`` during teardown reports
        ``down`` with no fault, never ``down`` alongside a crash/failed fault.
        """
        self._ready.clear()
        self._state = MpvState.DOWN
        self._fault = None
        await self._quit_and_close()
        self._discard_proc()

    async def _quit_and_close(self) -> None:
        """Ask mpv to quit and close the socket, ignoring a dead connection."""
        client = self._client
        self._client = None
        if client is None:
            return
        with contextlib.suppress(Exception):
            client.write_command(MpvCommand.quit())
        with contextlib.suppress(Exception):
            await client.close()

    def _discard_proc(self) -> None:
        """SIGKILL and forget the process (a dead or already-reaped one is a no-op)."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        with contextlib.suppress(ProcessLookupError):
            if proc.returncode is None:
                proc.kill()
