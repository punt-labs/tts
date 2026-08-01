"""Race a load's ended-future against a control interrupt.

``InterruptRace`` decides how a playing part *stopped* and returns a
:class:`TrackEnd`: a user interrupt (skip / off / play-a-part / switch) wins
outright; otherwise the load's ended-future resolved with an
:class:`~punt_vox.types_programs.mpv_event.EndFileReason` -- ``eof`` (advance),
``error`` (a bad file the loop records then advances past), or the synthetic
``crashed`` (mpv died; the loop replays the current part). The ended-future
always resolves with a *result*, never an exception -- a crash resolves it with
``crashed`` -- so the race never has to retrieve a raised awaitable. Extracting
it keeps :class:`~punt_vox.voxd.programs.loop.ProgramLoop` focused on *what to
do*, with the race mechanics owned here.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, Self, final

from punt_vox.voxd.programs.track_end import TrackEnd

if TYPE_CHECKING:
    from punt_vox.voxd.programs.player import PlayHandle

__all__ = ["InterruptRace"]


@final
class InterruptRace:
    """Settle how a part stopped: a control interrupt, or the load's end reason."""

    __slots__ = ("_interrupt",)
    _interrupt: asyncio.Event

    def __new__(cls, interrupt: asyncio.Event) -> Self:
        self = super().__new__(cls)
        self._interrupt = interrupt
        return self

    async def settle(self, handle: PlayHandle) -> TrackEnd:
        """Return the :class:`TrackEnd` describing how ``handle``'s load stopped.

        The load's end and the interrupt race; whichever completes first decides.
        A resolved ended-future wins with its reason (a real end, even ``crashed``,
        is authoritative); otherwise the interrupt won and the loop does not
        advance. The losing task is cancelled and reaped.
        """
        ended_task = asyncio.ensure_future(handle.ended())
        interrupt_task = asyncio.ensure_future(self._interrupt.wait())
        done, pending = await asyncio.wait(
            {ended_task, interrupt_task}, return_when=asyncio.FIRST_COMPLETED
        )
        await self._cancel_all(pending)
        if ended_task in done:
            return TrackEnd(interrupted=False, reason=ended_task.result())
        return TrackEnd(interrupted=True, reason=None)

    @staticmethod
    async def _cancel_all(tasks: set[asyncio.Task[Any]]) -> None:
        # Task[Any]: the race mixes a Task[EndFileReason] (handle.ended) and a
        # Task[bool] (Event.wait); this only cancels-and-reaps, so the type is moot.
        """Cancel and reap the losing tasks of the interrupt race."""
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
