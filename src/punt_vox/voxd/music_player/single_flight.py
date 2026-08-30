"""``SingleFlightRefresh`` -- run at most one background coroutine at a time.

:class:`~punt_vox.voxd.music_player.player.MusicPlayer` schedules its track-count
cache refresh from a synchronous hot path (the control-channel single-writer)
that must never await disk I/O itself. A burst of triggers -- one per completed
Part -- must not queue an unbounded pile of overlapping background reads, so
this holds the single-flight guard and the task's strong reference (asyncio
only weakly tracks a fire-and-forget task, and a collected one can vanish
mid-flight with a "Task was destroyed" warning) as its own small concern.

This is a drop-the-call guard, not a coalescing one: a :meth:`schedule` call
that arrives while a run is in flight is discarded outright -- not queued,
not merged with the run already executing. Reusing this class elsewhere is
only safe when the caller's ``work()`` re-reads whatever state it needs at
execution time rather than closing over a snapshot captured when
:meth:`schedule` was called -- see :meth:`schedule` for what that requires of
a caller.
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
    """Fire ``work`` in the background; a call while one runs is a no-op."""

    __slots__ = ("_running", "_task")
    _running: bool
    _task: asyncio.Task[None] | None

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._running = False
        self._task = None
        return self

    @property
    def running(self) -> bool:
        """Return whether a scheduled run is currently in flight."""
        return self._running

    def schedule(self, work: Callable[[], Coroutine[object, object, None]]) -> None:
        """Fire ``work()`` as a background task, unless one is already running.

        Never blocks the caller: this only creates the task and returns. A
        ``schedule()`` call while one is already in flight is dropped outright
        -- not queued, not merged -- so a caller relying on the in-flight run
        to "see" a dropped call's effect must ensure its ``work()`` re-reads
        live state at execution time rather than closing over a snapshot
        captured when ``schedule()`` was called. The current (and only)
        caller, :meth:`~punt_vox.voxd.music_player.player.MusicPlayer.
        _refresh_track_counts`, satisfies this because its resubmit reads
        ``self._latest_notice`` and the live catalog fresh at execution time --
        but that safety is a property of the caller, not of this class.

        The drop is logged at debug: a burst of routine coalescing during a
        normal fast refresh looks the same in the logs as a call dropped while
        the background refresh is stuck (or has been dead a long time) unless
        each drop leaves its own trace to compare timestamps against.
        """
        if self._running:
            logger.debug("single-flight: call dropped, a run is already in flight")
            return
        self._running = True
        self._task = asyncio.create_task(self._run(work))

    async def _run(self, work: Callable[[], Coroutine[object, object, None]]) -> None:
        """Run ``work``, then clear the guard regardless of outcome.

        A raising ``work`` is logged and swallowed here, not left to surface as
        an "exception was never retrieved" warning against the untracked task --
        nothing awaits it, by design, since scheduling is fire-and-forget.
        """
        try:
            await work()
        except Exception:
            logger.exception("single-flight background work failed")
        finally:
            self._running = False
            self._task = None
