"""``SingleFlightRefresh`` -- run at most one background coroutine at a time.

:class:`~punt_vox.voxd.music_player.player.MusicPlayer` schedules its track-count
cache refresh from a synchronous hot path (the control-channel single-writer)
that must never await disk I/O itself. A burst of triggers -- one per completed
Part -- must not queue an unbounded pile of overlapping background reads, so
this holds the single-flight guard and the task's strong reference (asyncio
only weakly tracks a fire-and-forget task, and a collected one can vanish
mid-flight with a "Task was destroyed" warning) as its own small concern,
reusable anywhere the same fire-and-coalesce shape is needed.
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

        Never blocks the caller: this only creates the task and returns. Later
        calls while it runs coalesce onto it -- the in-flight run will see
        whatever is true by the time its own work actually executes.
        """
        if self._running:
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
