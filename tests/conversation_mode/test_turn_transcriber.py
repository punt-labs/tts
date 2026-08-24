"""Tests for :class:`~.turn_transcriber.TurnTranscriber`."""

from __future__ import annotations

from conversation_mode._stt_fakes import FakeSTTProvider
from punt_vox.voxd.conversation_mode.audio_chunk import AudioChunk
from punt_vox.voxd.conversation_mode.stt_provider import TranscriptEvent
from punt_vox.voxd.conversation_mode.turn_transcriber import (
    CONFIDENCE_FLOOR,
    TurnTranscriber,
)


class _SpyTurnTimer:
    def __init__(self) -> None:
        self.marks: list[tuple[str, str | None]] = []

    def mark(self, stage: str, *, detail: str | None = None) -> None:
        self.marks.append((stage, detail))


def _one_chunk() -> list[AudioChunk]:
    return [AudioChunk(pcm=b"\x00\x00", duration_s=0.02)]


async def test_a_high_confidence_final_event_is_returned() -> None:
    timer = _SpyTurnTimer()
    stt = FakeSTTProvider(
        [TranscriptEvent(text="hello", confidence=0.95, is_final=True)]
    )
    transcriber = TurnTranscriber(stt, timer)

    result = await transcriber.transcribe(AudioChunk.as_async_iter(_one_chunk()))

    assert result is not None
    assert result.text == "hello"
    assert dict(timer.marks)["stt_response_received"] == "confidence=0.95"


async def test_a_low_confidence_final_event_returns_none() -> None:
    """FR-19: below the confidence floor, never fabricate."""
    timer = _SpyTurnTimer()
    stt = FakeSTTProvider(
        [
            TranscriptEvent(
                text="unsure", confidence=CONFIDENCE_FLOOR - 0.1, is_final=True
            )
        ]
    )
    transcriber = TurnTranscriber(stt, timer)

    result = await transcriber.transcribe(AudioChunk.as_async_iter(_one_chunk()))

    assert result is None


async def test_no_events_at_all_returns_none() -> None:
    timer = _SpyTurnTimer()
    stt = FakeSTTProvider([])
    transcriber = TurnTranscriber(stt, timer)

    result = await transcriber.transcribe(AudioChunk.as_async_iter(_one_chunk()))

    assert result is None
    assert dict(timer.marks)["stt_response_received"] == "no transcript"


async def test_a_transient_provider_failure_returns_none_not_raises() -> None:
    timer = _SpyTurnTimer()
    stt = FakeSTTProvider([], transcribe_error="rate limited")
    transcriber = TurnTranscriber(stt, timer)

    result = await transcriber.transcribe(AudioChunk.as_async_iter(_one_chunk()))

    assert result is None
    assert dict(timer.marks)["stt_response_received"] == "transcribe failed"
