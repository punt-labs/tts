"""``SingleFlightRefresh`` -- run at most one background coroutine at a time.

:class:`~punt_vox.voxd.music_player.player.MusicPlayer` schedules its track-count
cache refresh from a synchronous hot path (the control-channel single-writer)
that must never await disk I/O itself. A burst of triggers -- one per completed
Part -- must not queue an unbounded pile of overlapping background reads, so
this holds the single-flight guard and the task's strong reference (asyncio
only weakly tracks a fire-and-forget task, and a collected one can vanish
mid-flight with a "Task was destroyed" warning) as its own small concern.

A :meth:`schedule` call that arrives while a run is in flight never queues a
second task -- it sets a pending flag, and the run already executing loops
once more, immediately after it finishes, before clearing the guard. That
follow-up is what makes a dropped call's effect not lost: the extra pass
re-reads whatever state changed during the drop window, including the
window inside the caller's own ``work()`` -- not just the window between
:meth:`schedule` calls. A burst of several drops still collapses to exactly
one follow-up, never one per drop, because the pending marker is a flag, not
a counter. Reusing this class elsewhere is only safe when the caller's
``work()`` re-reads whatever state it needs at execution time rather than
closing over a snapshot captured when :meth:`schedule` was called -- see
:meth:`schedule` for what that requires of a caller.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

__all__ = ["SingleFlightRefresh"]

logger = logging.getLogger(__name__)


@final
class SingleFlightRefresh:
    """Fire ``work`` in the background; a call while one runs joins a follow-up."""

    __slots__ = ("_pending", "_running", "_task")
    _running: bool
    # Set by a schedule() call dropped while a run is in flight; consumed by
    # _run's loop, which clears it and runs work() once more before releasing
    # the guard. A flag, not a counter, so any number of drops in one run
    # still produce exactly one follow-up.
    _pending: bool
    _task: asyncio.Task[None] | None

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._running = False
        self._pending = False
        self._task = None
        return self

    @property
    def running(self) -> bool:
        """Return whether a scheduled run (or its pending follow-up) is in flight."""
        return self._running

    def schedule(self, work: Callable[[], Coroutine[object, object, None]]) -> None:
        """Fire ``work()`` in the background, or mark a follow-up if one is running.

        Never blocks the caller: this only creates the task (or flips the
        pending flag) and returns. A ``schedule()`` call while one is already
        in flight does not queue a second task -- it sets :attr:`_pending`,
        and the run already executing loops once more, immediately after it
        finishes, before releasing the guard. That follow-up re-reads
        whatever state changed during the drop, so a caller relying on it to
        "see" a dropped call's effect must ensure its ``work()`` re-reads live
        state at execution time rather than closing over a snapshot captured
        when ``schedule()`` was called. The current (and only) caller,
        :meth:`~punt_vox.voxd.music_player.player.MusicPlayer.
        _refresh_track_counts`, satisfies this because it reads the live
        catalog and ``self._latest_notice`` fresh at execution time -- but
        that safety is a property of the caller, not of this class.

        The drop is logged at debug: a burst of routine coalescing during a
        normal fast refresh looks the same in the logs as a call dropped while
        the background refresh is stuck (or has been dead a long time) unless
        each drop leaves its own trace to compare timestamps against.
        """
        if self._running:
            self._pending = True
            logger.debug("single-flight: call coalesced, a run is already in flight")
            return
        self._running = True
        self._task = asyncio.create_task(self._run(work))

    async def _run(self, work: Callable[[], Coroutine[object, object, None]]) -> None:
        """Run ``work``, looping once more per pending follow-up, then clear the guard.

        A raising ``work`` is logged and swallowed here, not left to surface as
        an "exception was never retrieved" warning against the untracked task --
        nothing awaits it, by design, since scheduling is fire-and-forget. The
        guard (:attr:`_running`) stays set across every follow-up loop, not
        just the first pass, so a schedule() call arriving during a follow-up
        coalesces into the next one rather than starting a second task.
        """
        try:
            while True:
                try:
                    await work()
                except Exception:
                    logger.exception("single-flight background work failed")
                if not self._pending:
                    break
                self._pending = False
        finally:
            self._running = False
            self._task = None
