"""Tests for :class:`AudioChunk`."""

from __future__ import annotations

from punt_vox.voxd.conversation_mode.audio_chunk import AudioChunk


def test_duration_and_pcm_round_trip() -> None:
    chunk = AudioChunk(pcm=b"\x01\x02", duration_s=0.02)
    assert chunk.pcm == b"\x01\x02"
    assert chunk.duration_s == 0.02


async def test_as_async_iter_yields_every_chunk_in_order() -> None:
    chunks = [
        AudioChunk(pcm=b"\x01", duration_s=0.02),
        AudioChunk(pcm=b"\x02", duration_s=0.02),
        AudioChunk(pcm=b"\x03", duration_s=0.02),
    ]

    collected = [chunk async for chunk in AudioChunk.as_async_iter(chunks)]

    assert collected == chunks


async def test_as_async_iter_of_an_empty_sequence_yields_nothing() -> None:
    collected = [chunk async for chunk in AudioChunk.as_async_iter([])]
    assert collected == []
