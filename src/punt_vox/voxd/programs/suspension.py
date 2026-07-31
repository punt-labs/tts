"""``PlaybackSuspension`` -- the daemon's click-free pause/resume seam.

Pause does not freeze the player in place. Freezing a live player with
``SIGSTOP`` cannot cleanly stop the audio device -- it underruns on pause and
re-primes glitchily on resume, and the clicks compound across pause/resume
cycles. Instead pause *tears the player down gracefully* (a ``SIGTERM`` the
player handles by closing its audio device cleanly, so the device stops with no
underrun) and *remembers where it was*: the part and its elapsed offset, held in
a :class:`ResumePoint`. Resume re-spawns the player seeked to that offset, so
playback continues from where it stopped with no click on either edge.

The suspension holds one gate the playback loop waits on. Pause clears it, so
the loop parks *without spawning the next part* -- there is no running player
while paused, so the part cursor cannot auto-advance (Z ``T3``). Resume opens it,
and the loop re-reads the (possibly ``prev``/``next``-moved) cursor and spawns it
at the recorded offset. Because a paused source has no live player, ``prev``/
``next`` reposition it by moving the cursor alone; the loop plays the newly
cursored part on resume, at offset~0 when the cursor moved and at the frozen
offset when it did not (:meth:`ResumePoint.offset_for`, Z Fork~B).

The suspension is shared by the loop (which ``attach``es each freshly spawned
handle with the offset it was seeked to, and ``detach``es it when the track
settles) and the ``ProgramService`` (which drives ``pause``/``resume`` and reads
:attr:`is_paused` for the status projection). It is the one place the paused flag
lives, so ``status`` reads it authoritatively.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd.programs.resume_point import LiveTrack

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.voxd.programs.part import Part
    from punt_vox.voxd.programs.player import PlayerProcess
    from punt_vox.voxd.programs.resume_point import ResumePoint

__all__ = ["PlaybackSuspension"]


@final
class PlaybackSuspension:
    """Hold the paused flag, the loop gate, the live handle, and the resume point."""

    __slots__ = ("_clock", "_gate", "_handle", "_live", "_paused", "_resume")
    _paused: bool
    _gate: asyncio.Event
    _clock: Callable[[], float]
    # The player process the loop is currently racing; None between tracks.
    _handle: PlayerProcess | None
    # The timing of that live handle; None when nothing is playing.
    _live: LiveTrack | None
    # Where the frozen part should resume; None when there is nothing to resume.
    _resume: ResumePoint | None

    def __new__(cls, clock: Callable[[], float] = time.monotonic) -> Self:
        self = super().__new__(cls)
        self._paused = False
        self._gate = asyncio.Event()
        self._gate.set()  # not paused -> the loop may spawn the next Part
        self._clock = clock
        self._handle = None
        self._live = None
        self._resume = None
        return self

    @property
    def is_paused(self) -> bool:
        """Return whether the active source is held (torn down, cursor frozen)."""
        return self._paused

    def seek_for(self, target: Part) -> float:
        """Return the offset the loop should seek ``target`` to when spawning it.

        The frozen offset when ``target`` is the paused part being resumed; ``0``
        for a fresh part or a part the cursor moved to while paused.
        """
        if self._resume is None:
            return 0.0
        return self._resume.offset_for(target)

    def pause(self) -> None:
        """Freeze the source: record where it is, tear the player down. Idempotent.

        The elapsed offset is frozen *before* the handle is stopped, from the live
        track's timing, so resume can seek back to it. The graceful stop lets the
        player close its audio device cleanly -- no underrun click.
        """
        if self._paused:
            return
        self._paused = True
        self._gate.clear()
        if self._live is not None:
            self._resume = self._live.resume_point(self._clock())
        if self._handle is not None:
            self._handle.stop_gracefully()

    def resume(self) -> None:
        """Open the loop gate so it re-spawns at the frozen offset. Idempotent."""
        if not self._paused:
            return
        self._paused = False
        self._gate.set()

    def reset(self) -> None:
        """Return to the not-paused, unheld, no-resume state (a stop or a switch).

        The frozen resume point is dropped: a stop or a source switch starts the
        next source fresh, never at a displaced album's offset. Runs at the
        daemon's source-lifecycle boundary (off, switch).
        """
        self._paused = False
        self._gate.set()
        self._handle = None
        self._live = None
        self._resume = None

    def attach(self, handle: PlayerProcess, part: Part, offset: float) -> None:
        """Register a freshly spawned handle and start its playback clock.

        ``offset`` is the seek the loop spawned it at (0 for a fresh part, the
        frozen offset for a resumed one), so the elapsed-offset accounting stays
        correct across repeated pause/resume cycles. If ``pause`` landed while the
        spawn was in flight -- the gate held the *next* spawn, but this one was
        already launching -- the handle is stopped at once so it never plays, and
        the resume point is refreshed to this part.
        """
        self._handle = handle
        self._live = LiveTrack(part, offset, self._clock())
        if self._paused:
            self._resume = self._live.resume_point(self._clock())
            handle.stop_gracefully()

    def detach(self) -> None:
        """Forget the live handle once its track has settled (killed or ended).

        A natural end (not paused) also drops the resume point, so the next
        (advanced) part starts fresh; a pause keeps the point ``pause`` recorded.
        """
        self._handle = None
        self._live = None
        if not self._paused:
            self._resume = None

    def shutdown(self) -> None:
        """Kill the held player on daemon stop so no orphan lingers.

        ``terminate`` is a synchronous ``SIGKILL``, so ``shutdown`` (which runs
        outside the event loop) tears the player down without awaiting. A paused
        source has already torn its player down, so there is usually nothing to
        kill -- but a player caught mid-spawn is stopped here too.
        """
        handle = self._handle
        self.reset()
        if handle is not None:
            handle.terminate()

    async def wait_resumed(self) -> None:
        """Block the playback loop while paused; return at once when playing."""
        await self._gate.wait()
