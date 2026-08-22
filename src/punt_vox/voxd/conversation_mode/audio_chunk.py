"""One slice of captured microphone audio, with caller-supplied timing."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["AudioChunk"]


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """16-bit signed mono PCM plus how long it spans, as the caller measured it.

    ``duration_s`` is a parameter, never a wall-clock read -- the turn and
    barge-in detectors (``docs/conversation-mode-prd.tex`` Chapter 3's
    testability requirements) must accept synthetic timing in tests, so
    nothing downstream of capture may call ``time.monotonic()`` itself.
    """

    pcm: bytes
    duration_s: float
