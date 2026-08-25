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


class TestMicAudioSourceDrainPending:
    async def test_noop_before_chunks_has_started(self) -> None:
        source = MicAudioSource(input_stream_factory=fake_input_stream_factory([]))
        assert source.drain_pending() == 0

    async def test_noop_after_chunks_has_finished(self) -> None:
        created: list[FakeInputStream] = []
        source = MicAudioSource(input_stream_factory=fake_input_stream_factory(created))
        gen = source.chunks()
        task = asyncio.ensure_future(gen.__anext__())
        await _advance_to_first_await()
        created[0].feed(b"\x00\x00")
        await task
        await gen.aclose()

        assert source.drain_pending() == 0

    async def test_discards_chunks_queued_but_not_yet_consumed(self) -> None:
        created: list[FakeInputStream] = []
        source = MicAudioSource(input_stream_factory=fake_input_stream_factory(created))
        gen = source.chunks()
        task = asyncio.ensure_future(gen.__anext__())
        await _advance_to_first_await()
        created[0].feed(b"\x01\x00")
        await task  # this one is consumed -- not part of the backlog

        # Two more arrive while nobody is pulling from the generator, e.g.
        # while the caller is busy elsewhere.
        created[0].feed(b"\x02\x00")
        created[0].feed(b"\x03\x00")
        await _advance_to_first_await()  # let call_soon_threadsafe land both puts

        assert source.drain_pending() == 2

        # The next real chunk still flows through normally after a drain.
        next_task = asyncio.ensure_future(gen.__anext__())
        await _advance_to_first_await()
        created[0].feed(b"\x04\x00")
        next_chunk = await next_task
        assert next_chunk.pcm == b"\x04\x00"

        await gen.aclose()

    async def test_returns_zero_when_the_queue_is_already_empty(self) -> None:
        created: list[FakeInputStream] = []
        source = MicAudioSource(input_stream_factory=fake_input_stream_factory(created))
        gen = source.chunks()
        task = asyncio.ensure_future(gen.__anext__())
        await _advance_to_first_await()
        created[0].feed(b"\x00\x00")
        await task

        assert source.drain_pending() == 0
        await gen.aclose()


class TestMicAudioSourceSetListening:
    async def test_defaults_to_listening(self) -> None:
        created: list[FakeInputStream] = []
        source = MicAudioSource(input_stream_factory=fake_input_stream_factory(created))
        gen = source.chunks()
        task = asyncio.ensure_future(gen.__anext__())
        await _advance_to_first_await()
        created[0].feed(b"\x01\x00")
        chunk = await task
        assert chunk.pcm == b"\x01\x00"
        await gen.aclose()

    async def test_chunks_are_dropped_at_the_source_while_not_listening(self) -> None:
        created: list[FakeInputStream] = []
        source = MicAudioSource(input_stream_factory=fake_input_stream_factory(created))
        gen = source.chunks()
        task = asyncio.ensure_future(gen.__anext__())
        await _advance_to_first_await()

        source.set_listening(listening=False)
        created[0].feed(b"\x01\x00")
        await _advance_to_first_await()

        # drain_pending proves nothing reached the queue -- not merely that
        # the pending anext() hasn't resolved yet.
        assert source.drain_pending() == 0

        source.set_listening(listening=True)
        created[0].feed(b"\x02\x00")
        chunk = await task
        assert chunk.pcm == b"\x02\x00"
        await gen.aclose()

    async def test_multiple_dropped_chunks_leave_no_backlog(self) -> None:
        created: list[FakeInputStream] = []
        source = MicAudioSource(input_stream_factory=fake_input_stream_factory(created))
        gen = source.chunks()
        task = asyncio.ensure_future(gen.__anext__())
        await _advance_to_first_await()

        source.set_listening(listening=False)
        created[0].feed(b"\x01\x00")
        created[0].feed(b"\x02\x00")
        created[0].feed(b"\x03\x00")
        await _advance_to_first_await()
        assert source.drain_pending() == 0

        source.set_listening(listening=True)
        created[0].feed(b"\x04\x00")
        chunk = await task
        assert chunk.pcm == b"\x04\x00"
        await gen.aclose()

    async def test_overlapping_closers_keep_the_mic_gated_until_both_reopen(
        self,
    ) -> None:
        """Two independent wrappers around ``set_listening``
        (``_speak_and_gate`` and ``_chime_and_gate`` in
        ``commands/call_live_driver.py``) don't currently overlap in
        production, but only by an unenforced ordering accident -- nothing
        here should assume they never will. Simulates the overlap directly:
        wrapper A closes, wrapper B closes, wrapper B reopens (its own
        scope ends first) -- the mic must stay gated because A's own scope
        is still open, not because of any incidental call ordering.
        """
        created: list[FakeInputStream] = []
        source = MicAudioSource(input_stream_factory=fake_input_stream_factory(created))
        gen = source.chunks()
        task = asyncio.ensure_future(gen.__anext__())
        await _advance_to_first_await()

        source.set_listening(listening=False)  # wrapper A closes
        source.set_listening(listening=False)  # wrapper B closes, while A is open
        created[0].feed(b"\x01\x00")
        await _advance_to_first_await()
        assert source.drain_pending() == 0

        source.set_listening(listening=True)  # wrapper B reopens; A is still open
        created[0].feed(b"\x02\x00")
        await _advance_to_first_await()
        # Still gated: a bare bool would have reopened the mic here, on B's
        # own listening=True, even though A's own scope never closed.
        assert source.drain_pending() == 0

        source.set_listening(listening=True)  # wrapper A reopens
        created[0].feed(b"\x03\x00")
        chunk = await task
        assert chunk.pcm == b"\x03\x00"
        await gen.aclose()

    async def test_listening_true_past_zero_does_not_go_negative(self) -> None:
        """An extra ``listening=True`` beyond the outstanding closes must not
        push the depth counter negative and require two closes to re-gate."""
        created: list[FakeInputStream] = []
        source = MicAudioSource(input_stream_factory=fake_input_stream_factory(created))
        gen = source.chunks()
        task = asyncio.ensure_future(gen.__anext__())
        await _advance_to_first_await()

        source.set_listening(listening=True)  # spurious reopen, already open

        source.set_listening(listening=False)
        created[0].feed(b"\x01\x00")
        await _advance_to_first_await()
        assert source.drain_pending() == 0

        source.set_listening(listening=True)
        created[0].feed(b"\x02\x00")
        chunk = await task
        assert chunk.pcm == b"\x02\x00"
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


class TestMicAudioSourceDeviceOpenFailure:
    """The single most likely real-world failure on a fresh machine -- no
    microphone present, permission denied, or the device already in
    use by another process. ``sounddevice.RawInputStream`` (or any factory
    substituting for it) raises when it cannot open the device; that
    exception must propagate out of :meth:`MicAudioSource.chunks` with a
    readable message, not get swallowed or corrupted into an opaque
    traceback -- ``commands/call.py``'s outer boundary handler speaks
    ``str(exc)`` verbatim to the human on the other end of the call.
    """

    def _raising_factory(self, message: str) -> object:
        def factory(**_kwargs: object) -> FakeInputStream:
            raise OSError(message)

        return factory

    async def test_a_device_open_failure_propagates_out_of_chunks(self) -> None:
        source = MicAudioSource(
            input_stream_factory=self._raising_factory(  # type: ignore[arg-type]
                "no default input device available"
            )
        )
        gen = source.chunks()
        with pytest.raises(OSError, match="no default input device available"):
            await gen.__anext__()
        # drain_pending()'s contract is a no-op before capture starts or
        # after it ends -- a factory failure never got capture started, so
        # _queue must go back to None, not stay wedged "on" forever.
        assert source.drain_pending() == 0

    async def test_the_propagated_error_message_is_human_readable(self) -> None:
        """The exact scenario ``commands/call.py``'s outer boundary handler
        turns into a spoken summary: ``str(exc)`` must be a plain sentence,
        not a repr or an empty string a human on the call would hear as
        "The call ended unexpectedly: . Check the terminal for details."
        """
        source = MicAudioSource(
            input_stream_factory=self._raising_factory(  # type: ignore[arg-type]
                "device or resource busy"
            )
        )
        gen = source.chunks()
        try:
            await gen.__anext__()
        except OSError as exc:
            message = str(exc)
        else:
            pytest.fail("expected the device-open failure to raise")
        assert message
        assert "device or resource busy" in message
