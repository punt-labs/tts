"""A :class:`~.playback_sink.PlaybackSink`'s observable lifecycle state."""

from __future__ import annotations

from enum import Enum, auto

__all__ = ["SinkStatus"]


class SinkStatus(Enum):
    """Where a sink is in its write/clear/close lifecycle.

    ``CLOSED`` is terminal: per ``docs/conversation-mode-call-state.tex``
    section 9, a closed sink accepts no further :meth:`~.playback_sink.
    PlaybackSink.write` or :meth:`~.playback_sink.PlaybackSink.clear` calls.
    """

    IDLE = auto()
    WRITING = auto()
    CLOSED = auto()
