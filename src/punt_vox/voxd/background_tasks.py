"""The daemon's background task set: started in order, cancelled in reverse."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

logger = logging.getLogger(__name__)

type _Job = Callable[[], Coroutine[object, object, None]]


@final
class BackgroundTasks:
    """Start a set of coroutines and cancel them in reverse on teardown.

    Teardown order is the reverse of start order, which is what the daemon
    needs: the playback consumer starts first and stops last, so the tasks that
    feed it are already gone before it goes. Reversing automatically means the
    order cannot drift out of step with the start order the way two
    hand-maintained lists do.
    """

    __slots__ = ("_tasks",)
    _tasks: list[asyncio.Task[None]]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._tasks = []
        return self

    def start(self, job: _Job) -> None:
        """Schedule ``job`` and hold its task for teardown."""
        self._tasks.append(asyncio.create_task(job()))

    async def stop_all(self) -> None:
        """Cancel every task in reverse start order, awaiting each exit."""
        for task in reversed(self._tasks):
            await self._stop(task)
        self._tasks.clear()

    @staticmethod
    async def _stop(task: asyncio.Task[None]) -> None:
        """Cancel one task and await its exit, swallowing the teardown.

        ``CancelledError`` is a ``BaseException`` (not ``Exception``) since 3.8,
        so it must be suppressed explicitly -- otherwise the *expected* cancel
        of the first task would propagate out of the caller's ``finally`` and
        skip every remaining teardown step.
        """
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
