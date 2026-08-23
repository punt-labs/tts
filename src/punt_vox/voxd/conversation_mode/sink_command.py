"""The ``SinkCommand`` interface -- one serialized operation on a sink.

Mirrors :class:`~.call_command.CallCommand`'s shape exactly, for the same
reason: :class:`~.playback_sink_actor.PlaybackSinkActor` calls
:meth:`SinkCommand.apply` on whatever it dequeues next; it never inspects a
command's type. Unlike ``CallCommand.apply`` (a synchronous state mutation),
``SinkCommand.apply`` is a coroutine -- writing, clearing, or closing a real
audio stream is I/O. The actor's serialization guarantee does not require
"nothing awaited mid-operation" the way ``CallActor`` documents for its
in-memory state machine; it requires the weaker, still sufficient property
that one command's :meth:`apply` -- awaits and all -- runs to completion
before the next command's :meth:`apply` begins. See
``docs/conversation-mode-call-state.tex`` section 9 for why that weaker
property is enough to resolve the three races the design names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.playback_sink import PlaybackSink

__all__ = ["SinkCommand"]


@runtime_checkable
class SinkCommand(Protocol):
    """A single command applied to a :class:`PlaybackSink` by the sole consumer.

    Its ``apply`` mirrors :meth:`~.call_command.CallCommand.apply` in
    shape, not signature.
    """

    async def apply(self, sink: PlaybackSink) -> None:
        """Apply this command's operation to *sink*."""
        ...
