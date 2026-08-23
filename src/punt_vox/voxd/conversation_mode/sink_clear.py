"""The ``SinkClear`` command: silence whatever is buffered right now."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.playback_sink import PlaybackSink

__all__ = ["SinkClear"]


@final
@dataclass(frozen=True, slots=True)
class SinkClear:
    """The barge-in primitive: drop buffered audio without closing the stream.

    Idempotent by contract (:meth:`~.playback_sink.PlaybackSink.clear`), so
    two ``SinkClear`` commands enqueued back to back -- a stop-during-a-stop,
    ``docs/conversation-mode-call-state.tex`` section 9 -- are both safe to
    apply in sequence: the second finds the sink already idle and is a no-op.
    *reason* is a diagnostic string only; it does not change the operation's
    effect.
    """

    reason: str

    async def apply(self, sink: PlaybackSink) -> None:
        await sink.clear()
