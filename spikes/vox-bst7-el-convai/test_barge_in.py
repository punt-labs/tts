"""Offline tests for the SyntheticAudio seam the barge-in run injects through."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Self, final

from barge_in import SyntheticAudio
from convai import EventTrace

if TYPE_CHECKING:
    from pathlib import Path

_CHUNK_BYTES = 2_048


@final
class _ChunkRecorder:
    """Stands in for the session's send_audio_chunk during pump tests."""

    _chunks: list[bytes]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._chunks = []
        return self

    @property
    def chunks(self) -> list[bytes]:
        return self._chunks

    async def send_audio_chunk(self, pcm: bytes) -> None:
        self._chunks.append(pcm)


def _mic(tmp_path: Path) -> SyntheticAudio:
    return SyntheticAudio(EventTrace(tmp_path / "trace.jsonl"))


class TestSpeakChunking:
    """Utterance PCM is streamed as uniform 64ms frames."""

    async def test_tail_chunk_is_padded_to_frame_size(self, tmp_path: Path) -> None:
        mic = _mic(tmp_path)
        recorder = _ChunkRecorder()
        mic.speak(b"\x01" * (_CHUNK_BYTES + 100))
        mic.start(recorder)
        await mic.wait_spoken()
        await mic.stop()
        spoken = [c for c in recorder.chunks if any(c)]
        assert [len(c) for c in spoken] == [_CHUNK_BYTES, _CHUNK_BYTES]

    async def test_pump_streams_silence_when_idle(self, tmp_path: Path) -> None:
        mic = _mic(tmp_path)
        recorder = _ChunkRecorder()
        mic.start(recorder)
        await asyncio.sleep(0.2)
        await mic.stop()
        assert recorder.chunks
        assert all(not any(c) for c in recorder.chunks)


class TestInterruptionObservation:
    """flush() is the live barge-in signal."""

    async def test_flush_sets_interrupted(self, tmp_path: Path) -> None:
        mic = _mic(tmp_path)
        await mic.flush()
        assert await mic.wait_interrupted(timeout_s=0.1) is True
        assert mic.interruption_count == 1

    async def test_no_flush_times_out_false(self, tmp_path: Path) -> None:
        mic = _mic(tmp_path)
        assert await mic.wait_interrupted(timeout_s=0.05) is False

    async def test_play_counts_agent_audio(self, tmp_path: Path) -> None:
        mic = _mic(tmp_path)
        await mic.play(b"\x00" * 320)
        assert mic.agent_audio_bytes == 320
