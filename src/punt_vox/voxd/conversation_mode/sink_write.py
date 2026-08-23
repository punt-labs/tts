"""The ``SinkWrite`` command: append synthesized audio to the sink's buffer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.playback_sink import PlaybackSink

__all__ = ["SinkWrite"]


@final
@dataclass(frozen=True, slots=True)
class SinkWrite:
    """Append *chunk* to whatever is currently playing.

    The synthesis pipeline that produces these commands is the sentence-
    streamed one: each synthesized segment becomes one ``SinkWrite``,
    enqueued as it completes -- never written to the sink directly.
    """

    chunk: bytes

    async def apply(self, sink: PlaybackSink) -> None:
        await sink.write(self.chunk)
