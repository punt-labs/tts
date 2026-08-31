"""The daemon's background task set: started in order, cancelled in reverse."""

from __future__ import annotations

import asyncio
import logging

# Runtime imports, not TYPE_CHECKING: the PEP 695 alias below is lazy, so a
# gated import survives module import and then raises NameError the moment
# anything resolves the alias -- typing.get_type_hints on ``start``, a doc
# tool, any runtime annotation reader.
from collections.abc import Callable, Coroutine
from typing import Self, final

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
        """Cancel one task and await its exit; report anything unexpected.

        ``CancelledError`` is a ``BaseException`` (not ``Exception``) since 3.8,
        so it must be caught explicitly -- otherwise the *expected* cancel of
        the first task would propagate out of the caller's ``finally`` and skip
        every remaining teardown step.

        Any other exception means the task had ALREADY failed before the cancel
        reached it. That is not a teardown detail, it is a background job that
        died, and swallowing it silently is how such a death goes unnoticed
        until something downstream behaves oddly. It is logged and not
        re-raised: teardown continuity still matters more than propagating one
        task's failure into the shutdown path.
        """
        task.cancel()
        # ``asyncio.wait`` returns when the task settles WITHOUT re-raising what
        # it settled with, so the outcome can be inspected rather than caught.
        # Awaiting the task directly would raise -- CancelledError for the
        # ordinary case, and anything else the task had already failed with --
        # which is what forced the old blanket ``suppress(Exception)`` here.
        await asyncio.wait({task})
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning(
                "background task %s failed before shutdown cancelled it",
                task.get_name(),
                exc_info=exc,
            )
