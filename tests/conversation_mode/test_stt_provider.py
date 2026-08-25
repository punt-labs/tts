"""Tests for the ``STTProvider`` protocol conformance and the FR-19 confidence gate.

FR-19: the system shall never fabricate or guess at what the human said when
audio is ambiguous or capture fails; it shall ask the human to repeat rather
than act on a low-confidence guess. This module tests the fake in isolation;
``tests/conversation_mode/test_call_orchestrator.py`` exercises the same gate
wired into the full pipeline.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from conversation_mode._stt_fakes import FakeSTTProvider
from punt_vox.voxd.conversation_mode.audio_chunk import AudioChunk
from punt_vox.voxd.conversation_mode.stt_provider import STTProvider, TranscriptEvent

# FR-19's action threshold: an event at or above this confidence may be acted
# on; below it, the caller must ask the human to repeat rather than guess.
CONFIDENCE_FLOOR = 0.6


async def _no_chunks() -> AsyncIterator[AudioChunk]:
    return
    yield  # pragma: no cover -- makes this an async generator with no items


def test_fake_satisfies_the_stt_provider_protocol() -> None:
    fake = FakeSTTProvider([])
    assert isinstance(fake, STTProvider)


async def test_transcribe_replays_the_scripted_events_in_order() -> None:
    script = [
        TranscriptEvent(text="hello", confidence=0.4, is_final=False),
        TranscriptEvent(text="hello there", confidence=0.95, is_final=True),
    ]
    fake = FakeSTTProvider(script)
    events = [event async for event in fake.transcribe(_no_chunks())]
    assert events == script


async def test_low_confidence_final_event_must_not_be_acted_on() -> None:
    """FR-19: a low-confidence final transcript is a signal to ask, not act."""
    fake = FakeSTTProvider(
        [TranscriptEvent(text="turn on the lights", confidence=0.2, is_final=True)]
    )
    events = [event async for event in fake.transcribe(_no_chunks())]
    (final,) = events
    assert final.is_final
    assert final.confidence < CONFIDENCE_FLOOR


async def test_high_confidence_final_event_may_be_acted_on() -> None:
    fake = FakeSTTProvider(
        [TranscriptEvent(text="turn on the lights", confidence=0.92, is_final=True)]
    )
    events = [event async for event in fake.transcribe(_no_chunks())]
    (final,) = events
    assert final.is_final
    assert final.confidence >= CONFIDENCE_FLOOR


def test_confidence_must_be_in_unit_interval() -> None:
    with pytest.raises(ValueError, match=r"\[0\.0, 1\.0\]"):
        TranscriptEvent(text="x", confidence=1.5, is_final=True)
    with pytest.raises(ValueError, match=r"\[0\.0, 1\.0\]"):
        TranscriptEvent(text="x", confidence=-0.1, is_final=True)


async def test_fake_records_how_many_chunks_it_was_given() -> None:
    async def two_chunks() -> AsyncIterator[AudioChunk]:
        yield AudioChunk(pcm=b"\x00\x00", duration_s=0.02)
        yield AudioChunk(pcm=b"\x00\x00", duration_s=0.02)

    fake = FakeSTTProvider([])
    async for _ in fake.transcribe(two_chunks()):
        pass
    assert fake.calls() == [2]
