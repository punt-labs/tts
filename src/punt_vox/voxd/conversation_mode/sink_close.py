"""The ``SinkClose`` command: release the stream. Terminal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.playback_sink import PlaybackSink

__all__ = ["SinkClose"]


@final
@dataclass(frozen=True, slots=True)
class SinkClose:
    """End the call's audio stream; no further command is valid after this."""

    async def apply(self, sink: PlaybackSink) -> None:
        await sink.close()
