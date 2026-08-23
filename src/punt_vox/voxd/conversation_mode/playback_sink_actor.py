"""The single serialized dispatch point for one call's :class:`PlaybackSink`.

``docs/conversation-mode-call-state.tex`` section 9 resolves the three
concurrency questions the barge-in design left open (a natural-completion
vs. barge-in race, a stop-during-a-stop, and clear-vs-concurrent-writer) by
reusing :class:`~.call_actor.CallActor`'s discipline: an actor with an
internal, single-consumer :class:`asyncio.Queue` of commands, not a lock.
The synthesis pipeline (writes) and the barge-in detector (clears) each
hold a reference to this actor and call :meth:`enqueue`; neither ever calls
:meth:`~.playback_sink.PlaybackSink.write` or
:meth:`~.playback_sink.PlaybackSink.clear` on the sink directly. Exactly one
task drains the queue, applying one command's :meth:`~.sink_command.
SinkCommand.apply` -- including whatever it awaits -- to completion before
the next command is even dequeued. That is what makes "one operation in
flight at a time" true of the sink, no matter how many producers are racing
to enqueue against it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd.conversation_mode.sink_status import SinkStatus

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.playback_sink import PlaybackSink
    from punt_vox.voxd.conversation_mode.sink_command import SinkCommand

logger = logging.getLogger(__name__)

__all__ = ["PlaybackSinkActor"]


@final
class PlaybackSinkActor:
    """Owns one :class:`PlaybackSink`, draining commands against it one at a time."""

    __slots__ = ("_queue", "_sink")
    _sink: PlaybackSink
    _queue: asyncio.Queue[SinkCommand | None]

    def __new__(cls, sink: PlaybackSink) -> Self:
        self = super().__new__(cls)
        self._sink = sink
        self._queue = asyncio.Queue()
        return self

    @property
    def status(self) -> SinkStatus:
        """Return the underlying sink's current lifecycle state."""
        return self._sink.status

    async def enqueue(self, command: SinkCommand) -> None:
        """Queue *command* for the dispatch loop; returns once it is queued.

        Every producer -- the synthesis pipeline, the barge-in detector --
        calls only this. None ever calls :meth:`~.playback_sink.
        PlaybackSink.write`, :meth:`~.playback_sink.PlaybackSink.clear`, or
        :meth:`~.playback_sink.PlaybackSink.close` on the sink itself; doing
        so would bypass the serialization this actor exists to provide.
        """
        await self._queue.put(command)

    async def run(self) -> None:
        """Drain the command queue until :meth:`stop` is called.

        Awaits each command's :meth:`~.sink_command.SinkCommand.apply` to
        full completion -- not merely scheduled, not fired-and-forgotten --
        before dequeuing the next one. That sequencing is the entire
        correctness argument: two commands' effects on the sink can never
        interleave, because the second is not even dequeued until the
        first's ``apply`` coroutine has returned.

        A command's ``apply`` is isolated in its own ``try``/``except``:
        ``SinkWrite``/``SinkClear`` are documented to raise when
        :attr:`~.playback_sink.PlaybackSink.status` is
        :attr:`~.sink_status.SinkStatus.CLOSED`, a reachable condition, and
        an uncaught raise here would kill this loop silently (nothing
        supervises the task) while every producer waiting on
        :meth:`drain`/``queue.join`` blocked forever. ``task_done`` runs in a
        ``finally`` so that never happens, mirroring
        :meth:`~.call_actor.CallActor.apply`'s per-observer isolation for
        the same reason.
        """
        while True:
            command = await self._queue.get()
            if command is None:
                self._queue.task_done()
                return
            try:
                await command.apply(self._sink)
            except Exception:
                logger.exception("playback sink command %r raised", command)
            finally:
                self._queue.task_done()

    async def stop(self) -> None:
        """Signal :meth:`run` to return once it has drained pending commands."""
        await self._queue.put(None)

    async def drain(self) -> None:
        """Block until every command enqueued so far has been applied.

        Uses :meth:`asyncio.Queue.join`, which waits on ``task_done`` calls
        rather than queue emptiness -- so it also covers the command
        :meth:`run` is mid-:meth:`~.sink_command.SinkCommand.apply` on, not
        just what is still waiting in the queue.

        Known gap: a command :meth:`enqueue`-d *after* :meth:`stop` sits in
        the queue forever -- :meth:`run` has already returned and nothing
        will ever dequeue it, so this hangs indefinitely rather than
        raising. Not fixed here: this actor has no production caller yet
        (wiring it to a real synthesis pipeline and barge-in detector is
        deferred, tracked separately from this module), so there is no real
        producer today that could race a ``stop()``. Whoever wires the
        first caller needs to decide the right behavior -- reject a
        post-stop enqueue, or make ``drain`` itself bounded -- with real
        callers to design against.
        """
        await self._queue.join()
