"""Concurrency proofs for :class:`PlaybackSinkActor`.

Three tests exercise the three races
``docs/conversation-mode-call-state.tex`` section 9 names as needing
resolution before implementation: natural-completion-vs-barge-in,
stop-during-a-stop, and clear-vs-concurrent-writer. Each test runs many real
concurrent :class:`asyncio.Task` producers against one actor and asserts
:class:`~conversation_mode._playback_sink_fakes.SinkReentrancyError` never
fires -- the fake raises it the instant two operations are ever in flight
at once, so its absence is direct evidence of serialization, not an
inference from call counts.

A fourth test is the negative control: it bypasses the actor and calls the
same fake's methods directly from two concurrent tasks. That test asserts
the reentrancy error *does* fire -- proving the harness actually detects
the race it claims to prevent, not merely that concurrent code sometimes
runs without incident.
"""

from __future__ import annotations

import asyncio

import pytest

from conversation_mode._playback_sink_fakes import FakeSink, SinkReentrancyError
from punt_vox.voxd.conversation_mode.playback_sink_actor import PlaybackSinkActor
from punt_vox.voxd.conversation_mode.sink_clear import SinkClear
from punt_vox.voxd.conversation_mode.sink_status import SinkStatus
from punt_vox.voxd.conversation_mode.sink_write import SinkWrite

_TRIALS = 50


async def _run_actor(actor: PlaybackSinkActor) -> asyncio.Task[None]:
    """Start the actor's dispatch loop as a background task."""
    return asyncio.create_task(actor.run())


async def test_negative_control_bypassing_the_actor_reproduces_the_race() -> None:
    """Prove the fake's reentrancy detector is not a tautology.

    Two tasks call :meth:`FakeSink.write`/:meth:`FakeSink.clear` directly,
    with no actor serializing them -- exactly the mistake the actor exists
    to prevent. This must raise; if it stops raising, the fake has stopped
    detecting the race and every other test in this file is meaningless.
    """
    sink = FakeSink()

    async def writer() -> None:
        for _ in range(20):
            await sink.write(b"x")

    async def clearer() -> None:
        for _ in range(20):
            await sink.clear()

    with pytest.raises(ExceptionGroup) as excinfo:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(writer())
            tg.create_task(clearer())
    matched, _ = excinfo.value.split(SinkReentrancyError)
    assert matched is not None


async def test_natural_completion_vs_barge_in_race_never_interleaves() -> None:
    """A writer finishing its last chunk and a clearer racing it stay serialized.

    Regardless of which producer's command lands in the queue first on any
    given trial, the actor applies commands one at a time -- no reentrancy,
    and the sink always ends the trial in a well-defined state (idle,
    because the last command applied is always a clear).
    """
    for _ in range(_TRIALS):
        sink = FakeSink()
        actor = PlaybackSinkActor(sink)
        runner = await _run_actor(actor)

        async def completing_writer(actor: PlaybackSinkActor = actor) -> None:
            for i in range(5):
                await actor.enqueue(SinkWrite(chunk=bytes([i])))
                await asyncio.sleep(0)

        async def barging_clearer(actor: PlaybackSinkActor = actor) -> None:
            await asyncio.sleep(0)
            await actor.enqueue(SinkClear(reason="barge-in"))

        async with asyncio.TaskGroup() as tg:
            tg.create_task(completing_writer())
            tg.create_task(barging_clearer())
        # The clear is always the last command a test enqueues here, so
        # draining leaves the sink idle no matter how the writes and the
        # clear interleaved in the queue.
        await actor.enqueue(SinkClear(reason="settle"))
        await actor.drain()
        await actor.stop()
        await runner

        assert sink.status is SinkStatus.IDLE
        assert sink.buffered_bytes == 0


async def test_stop_during_a_stop_is_idempotent_and_never_reentrant() -> None:
    """Two barge-ins racing each other both apply safely; the sink ends idle."""
    for _ in range(_TRIALS):
        sink = FakeSink()
        actor = PlaybackSinkActor(sink)
        runner = await _run_actor(actor)
        await actor.enqueue(SinkWrite(chunk=b"partial-segment"))

        async def first_stop(actor: PlaybackSinkActor = actor) -> None:
            await actor.enqueue(SinkClear(reason="barge-in"))

        async def second_stop(actor: PlaybackSinkActor = actor) -> None:
            await actor.enqueue(SinkClear(reason="second barge-in"))

        async with asyncio.TaskGroup() as tg:
            tg.create_task(first_stop())
            tg.create_task(second_stop())
        await actor.drain()
        await actor.stop()
        await runner

        assert sink.status is SinkStatus.IDLE
        assert sink.buffered_bytes == 0
        assert sink.history.count("clear") == 2


async def test_buffer_clear_vs_concurrent_writer_stress() -> None:
    """Many concurrent writer and clearer tasks never corrupt the sink.

    The stand-in for "a real audio-driver thread writing while another
    thread clears": on one actor, several producer tasks hammer
    :meth:`~.playback_sink_actor.PlaybackSinkActor.enqueue` with writes
    while several others hammer it with clears, at full concurrency for
    many iterations. Zero reentrancy across the whole run is the proof that
    routing every producer through one actor -- instead of letting any of
    them touch the sink directly -- is what the ordering discipline claims.
    """
    sink = FakeSink()
    actor = PlaybackSinkActor(sink)
    runner = await _run_actor(actor)

    async def writer(tag: int) -> None:
        for i in range(30):
            await actor.enqueue(SinkWrite(chunk=bytes([tag, i % 256])))

    async def clearer(tag: int) -> None:
        for _ in range(30):
            await actor.enqueue(SinkClear(reason=f"clearer-{tag}"))
            await asyncio.sleep(0)

    async with asyncio.TaskGroup() as tg:
        for tag in range(4):
            tg.create_task(writer(tag))
        for tag in range(4):
            tg.create_task(clearer(tag))
    await actor.drain()
    await actor.stop()
    await runner

    # No assertion beyond "no exception raised" is needed: a
    # SinkReentrancyError would have propagated out of run() -> the task ->
    # this test, per the negative control above proving the detector works.
    assert sink.status in (SinkStatus.IDLE, SinkStatus.WRITING)
