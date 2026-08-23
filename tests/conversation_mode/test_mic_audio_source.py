"""Tests for :mod:`punt_vox.voxd.conversation_mode.mic_audio_source`.

No real audio hardware, no ``sounddevice`` C extension calls -- every test
drives :class:`MicAudioSource` through
:func:`conversation_mode._mic_fakes.fake_input_stream_factory`. Two-step
timing pattern used throughout: schedule the generator's first ``__anext__``
as a task, ``await asyncio.sleep(0)`` once to let it run synchronously up to
its first suspension point (``await queue.get()``), then feed the fake
stream and await the task -- mirroring how a real PortAudio callback thread
delivers audio asynchronously relative to the consuming coroutine.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from conversation_mode._mic_fakes import FakeInputStream, fake_input_stream_factory
from punt_vox.voxd.conversation_mode.audio_chunk import SAMPLE_RATE_HZ
from punt_vox.voxd.conversation_mode.mic_audio_source import MicAudioSource


async def _advance_to_first_await() -> None:
    await asyncio.sleep(0)


class TestMicAudioSourceChunks:
    async def test_yields_the_fed_pcm(self) -> None:
        created: list[FakeInputStream] = []
        source = MicAudioSource(input_stream_factory=fake_input_stream_factory(created))
        gen = source.chunks()
        task = asyncio.ensure_future(gen.__anext__())
        await _advance_to_first_await()

        (stream,) = created
        stream.feed(b"\x01\x00\x02\x00")
        chunk = await task
        assert chunk.pcm == b"\x01\x00\x02\x00"

        await gen.aclose()

    async def test_every_chunk_carries_the_configured_duration(self) -> None:
        created: list[FakeInputStream] = []
        source = MicAudioSource(
            chunk_s=0.05, input_stream_factory=fake_input_stream_factory(created)
        )
        gen = source.chunks()
        task = asyncio.ensure_future(gen.__anext__())
        await _advance_to_first_await()
        created[0].feed(b"\x00\x00")
        chunk = await task
        assert chunk.duration_s == pytest.approx(0.05)
        await gen.aclose()

    async def test_requests_the_configured_sample_rate_and_mono_channel(self) -> None:
        created: list[FakeInputStream] = []
        source = MicAudioSource(
            sample_rate_hz=22_050,
            input_stream_factory=fake_input_stream_factory(created),
        )
        gen = source.chunks()
        task = asyncio.ensure_future(gen.__anext__())
        await _advance_to_first_await()
        created[0].feed(b"\x00\x00")
        await task
        await gen.aclose()

        (stream,) = created
        assert stream.kwargs["samplerate"] == pytest.approx(22_050.0)
        assert stream.kwargs["channels"] == 1
        assert stream.kwargs["dtype"] == "int16"

    async def test_default_sample_rate_matches_the_shared_constant(self) -> None:
        created: list[FakeInputStream] = []
        source = MicAudioSource(input_stream_factory=fake_input_stream_factory(created))
        gen = source.chunks()
        task = asyncio.ensure_future(gen.__anext__())
        await _advance_to_first_await()
        created[0].feed(b"\x00\x00")
        await task
        await gen.aclose()

        assert created[0].kwargs["samplerate"] == pytest.approx(float(SAMPLE_RATE_HZ))

    async def test_closing_the_generator_releases_the_stream(self) -> None:
        created: list[FakeInputStream] = []
        source = MicAudioSource(input_stream_factory=fake_input_stream_factory(created))
        gen = source.chunks()
        task = asyncio.ensure_future(gen.__anext__())
        await _advance_to_first_await()
        created[0].feed(b"\x00\x00")
        await task

        assert created[0].closed is False
        await gen.aclose()
        assert created[0].closed is True

    async def test_input_overflow_is_logged_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        created: list[FakeInputStream] = []
        source = MicAudioSource(input_stream_factory=fake_input_stream_factory(created))
        gen = source.chunks()
        task = asyncio.ensure_future(gen.__anext__())
        await _advance_to_first_await()

        with caplog.at_level(logging.WARNING):
            created[0].feed(b"\x00\x00", input_overflow=True)
            chunk = await task

        assert chunk.pcm == b"\x00\x00"
        assert any("overflow" in record.message for record in caplog.records)
        await gen.aclose()

    async def test_multiple_feeds_yield_multiple_chunks_in_order(self) -> None:
        created: list[FakeInputStream] = []
        source = MicAudioSource(input_stream_factory=fake_input_stream_factory(created))
        gen = source.chunks()
        first_task = asyncio.ensure_future(gen.__anext__())
        await _advance_to_first_await()
        created[0].feed(b"\x01\x00")
        first_chunk = await first_task

        second_task = asyncio.ensure_future(gen.__anext__())
        await _advance_to_first_await()
        created[0].feed(b"\x02\x00")
        second_chunk = await second_task

        assert first_chunk.pcm == b"\x01\x00"
        assert second_chunk.pcm == b"\x02\x00"
        await gen.aclose()


class TestMicAudioSourceCaptureSeconds:
    async def test_captures_exactly_the_requested_chunk_count(self) -> None:
        created: list[FakeInputStream] = []
        source = MicAudioSource(
            chunk_s=0.02, input_stream_factory=fake_input_stream_factory(created)
        )
        task = asyncio.ensure_future(source.capture_seconds(0.1))
        for _ in range(5):
            await _advance_to_first_await()
            created[-1].feed(b"\x00\x00")
        collected = await task
        assert len(collected) == 5

    async def test_releases_its_own_stream_when_done(self) -> None:
        created: list[FakeInputStream] = []
        source = MicAudioSource(
            chunk_s=0.02, input_stream_factory=fake_input_stream_factory(created)
        )
        task = asyncio.ensure_future(source.capture_seconds(0.02))
        await _advance_to_first_await()
        created[0].feed(b"\x00\x00")
        await task
        assert created[0].closed is True

    async def test_at_least_one_chunk_for_a_sub_chunk_duration(self) -> None:
        created: list[FakeInputStream] = []
        source = MicAudioSource(
            chunk_s=0.02, input_stream_factory=fake_input_stream_factory(created)
        )
        task = asyncio.ensure_future(source.capture_seconds(0.001))
        await _advance_to_first_await()
        created[0].feed(b"\x00\x00")
        collected = await task
        assert len(collected) == 1
