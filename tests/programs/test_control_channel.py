"""Tests for the single-writer ControlChannel, including the O2 concurrency contract."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Self, cast, final

import pytest

from punt_vox.types_programs import MAX_RETRY, Mode, Reason
from punt_vox.types_programs.prompts import PromptSet
from punt_vox.voxd.programs import Part, PlaybackPolicy, Program, ProgramState
from punt_vox.voxd.programs.active_context import ActiveContext
from punt_vox.voxd.programs.album_id import AlbumId
from punt_vox.voxd.programs.album_tags import AlbumTags, PromptFingerprint
from punt_vox.voxd.programs.change_signal import ChangeSignal
from punt_vox.voxd.programs.control_channel import ControlChannel
from punt_vox.voxd.programs.control_signal import ControlSignal
from punt_vox.voxd.programs.filesystem_store import FilesystemProgramStore
from punt_vox.voxd.programs.fill_reconciler import FillReconciler
from punt_vox.voxd.programs.fill_signal import TransientFailure
from punt_vox.voxd.programs.filler import Filler, FillPlan
from punt_vox.voxd.programs.guard import GuardViolationError
from punt_vox.voxd.programs.lifecycle_signal import TurnOff, TurnOn, VibeStyleChange
from punt_vox.voxd.programs.manifest import ManifestDraft
from punt_vox.voxd.programs.playback_signal import Rotate, StepBack
from punt_vox.voxd.programs.producer import PartSpec
from punt_vox.voxd.programs.sleeper import Sleeper

from .conftest import AvoidRepeatPolicy

if TYPE_CHECKING:
    from punt_vox.voxd.programs.playback_source import PlaybackSource
    from punt_vox.voxd.programs.store import PartStore

PoolFactory = Callable[..., frozenset[Part]]
RotatingFactory = Callable[[PlaybackPolicy], Program]

_TAGS = AlbumTags(style="jazz", vibe="calm")
_PROMPTS = PromptSet(base="p", variations=())


def _prog(channel: ControlChannel) -> Program:
    """Return the channel's active source narrowed to a Program."""
    return cast("Program", channel.source)


class _ChangeRecorder:
    """A ChangeListener that counts change notifications."""

    def __init__(self) -> None:
        self.count = 0

    def notify_changed(self) -> None:
        self.count += 1


def _turn_off(channel: ControlChannel) -> TurnOff:
    """Build a source-agnostic TurnOff for a Program channel."""
    idle = Program(ProgramState.initial(), AvoidRepeatPolicy())
    return TurnOff(channel, ActiveContext(), idle)


def _make_store(root: Path) -> PartStore:
    """Create a fresh album directory and return its PartStore."""
    draft = ManifestDraft(
        album_id=AlbumId("a3f1c9"),
        tags=_TAGS,
        fingerprint=PromptFingerprint("deadbeef"),
    )
    return FilesystemProgramStore(root).create(draft)


def _plan(store: PartStore) -> FillPlan:
    """Return a fill plan over ``store`` with the shared tags and prompts."""
    return FillPlan(store, _TAGS, _PROMPTS)


async def _drain(channel: ControlChannel) -> None:
    """Run the consumer until the queue is empty, then stop it."""
    server = asyncio.create_task(channel.serve())
    await channel.join()
    server.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await server


class TestSingleWriter:
    async def test_applies_a_command(self, policy: PlaybackPolicy) -> None:
        channel = ControlChannel(Program(ProgramState.initial(), policy))
        channel.post(TurnOn())
        await channel.apply_next()
        assert _prog(channel).mode is Mode.GENERATING_FIRST

    async def test_changed_event_set_after_apply(self, policy: PlaybackPolicy) -> None:
        channel = ControlChannel(Program(ProgramState.initial(), policy))
        channel.post(TurnOn())
        await channel.apply_next()
        assert channel.changed.is_set()

    async def test_change_signal_fires_after_an_applied_command(
        self, policy: PlaybackPolicy
    ) -> None:
        # The scene re-push rides the single writer: after a command is applied
        # (serialized with the state it reports), the change signal fires once.
        channel = ControlChannel(Program(ProgramState.initial(), policy))
        signal = ChangeSignal()
        recorder = _ChangeRecorder()
        signal.subscribe(recorder)
        channel.attach_change_signal(signal)
        channel.post(TurnOn())
        await channel.apply_next()
        assert recorder.count == 1

    async def test_lost_race_guard_is_swallowed(self, policy: PlaybackPolicy) -> None:
        # turn_on twice: the second is a lost race (already on) -> ValueError,
        # swallowed so the single writer survives and the state is unchanged.
        channel = ControlChannel(Program(ProgramState.initial(), policy))
        channel.post(TurnOn())
        channel.post(TurnOn())
        await _drain(channel)
        assert _prog(channel).mode is Mode.GENERATING_FIRST

    async def test_serves_a_batch_in_order(
        self,
        make_rotating: RotatingFactory,
        policy: PlaybackPolicy,
        pool_of: PoolFactory,
    ) -> None:
        channel = ControlChannel(make_rotating(policy))
        channel.post(Rotate())
        channel.post(VibeStyleChange(pool_of(20, 21)))
        await _drain(channel)
        # Vibe applied last -> the pool is the retuned one.
        assert {p.index for p in _prog(channel).pool} == {20, 21}


class TestRejectedInterruptDoesNotFire:
    """A rejected interrupting command must NOT fire the interrupt.

    ``StepBack`` (transport prev) declares ``interrupts`` but rejects on a generate
    Program as a lost race. If the interrupt fired anyway, the loop would kill and
    re-spawn the current track from zero while the cursor stayed put -- a silent
    stutter whose only log says "nothing happened". ``changed`` still fires so the
    loop is never left blocked.
    """

    async def test_prev_on_a_program_is_rejected_without_interrupting(
        self, policy: PlaybackPolicy
    ) -> None:
        channel = ControlChannel(Program(ProgramState.initial(), policy))
        channel.interrupt.clear()
        channel.post(StepBack())  # prev has no meaning on a generate Program
        await channel.apply_next()
        assert not channel.interrupt.is_set()  # the current track is NOT re-spawned
        assert channel.changed.is_set()  # but the loop is still woken

    async def test_an_applied_interrupt_still_fires(
        self, make_rotating: RotatingFactory, policy: PlaybackPolicy
    ) -> None:
        # The positive control: a Rotate that APPLIES (a rotating pool) still fires
        # the interrupt, so the gate suppresses only the rejected case.
        channel = ControlChannel(make_rotating(policy))
        channel.interrupt.clear()
        channel.post(Rotate())
        await channel.apply_next()
        assert channel.interrupt.is_set()  # an applied user skip interrupts as before


class TestModeTransitionLogging:
    """apply_next logs a Program mode transition once, on change only."""

    async def test_mode_transition_logged_once(
        self, policy: PlaybackPolicy, caplog: pytest.LogCaptureFixture
    ) -> None:
        channel = ControlChannel(Program(ProgramState.initial(), policy))
        channel.post(TurnOn())
        with caplog.at_level(
            logging.INFO, logger="punt_vox.voxd.programs.mode_transition_log"
        ):
            await channel.apply_next()
        lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert lines == ["music: off → generating_first"]

    async def test_same_mode_advance_logs_nothing(
        self,
        make_rotating: RotatingFactory,
        policy: PlaybackPolicy,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A rotate within playing_rotating changes no mode -> no transition line."""
        channel = ControlChannel(make_rotating(policy))
        channel.post(Rotate())
        with caplog.at_level(
            logging.INFO, logger="punt_vox.voxd.programs.mode_transition_log"
        ):
            await channel.apply_next()
        transitions = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.INFO and "music:" in r.getMessage()
        ]
        assert transitions == []


class TestO2Concurrency:
    """Concurrent next + vibe never interleave: the result is one of the two
    valid sequential outcomes, and the Program is always a legal state."""

    async def test_concurrent_next_and_vibe_is_sequential(
        self,
        make_rotating: RotatingFactory,
        policy: PlaybackPolicy,
        pool_of: PoolFactory,
    ) -> None:
        new_pool = pool_of(20, 21)

        def sequential(*signals: ControlSignal) -> ProgramState:
            prog = make_rotating(policy)
            for signal in signals:
                signal.apply(prog)
            return prog.state

        rotate_then_vibe = sequential(Rotate(), VibeStyleChange(new_pool))
        vibe_then_rotate = sequential(VibeStyleChange(new_pool), Rotate())

        channel = ControlChannel(make_rotating(policy))
        server = asyncio.create_task(channel.serve())
        # Two "clients" fire concurrently; the channel serializes them.
        await asyncio.gather(
            _post_soon(channel, Rotate()),
            _post_soon(channel, VibeStyleChange(new_pool)),
        )
        await channel.join()
        server.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server

        result = _prog(channel).state
        assert result in {rotate_then_vibe, vibe_then_rotate}  # never interleaved
        # The pool is the retuned one under both orderings (rotate never repools).
        assert {p.index for p in _prog(channel).pool} == {20, 21}


async def _post_soon(channel: ControlChannel, signal: ControlSignal) -> None:
    await asyncio.sleep(0)
    channel.post(signal)


@final
class _HoldingProducer:
    """Never completes -- keeps the fill task alive so is_running stays True."""

    async def produce(self, spec: PartSpec, target: Path) -> Part:
        await asyncio.Event().wait()  # blocks forever
        return Part(target.name, spec.index)


@final
class _FixedPlanSource:
    __slots__ = ("_plan",)
    _plan: FillPlan

    def __new__(cls, plan: FillPlan) -> Self:
        self = super().__new__(cls)
        self._plan = plan
        return self

    def current_plan(self) -> FillPlan:
        return self._plan


class TestFillReconciliation:
    """The consumer starts/cancels the Filler to match program.filling."""

    async def test_filling_starts_and_off_cancels_the_fill(
        self,
        tmp_path: Path,
        policy: PlaybackPolicy,
        sleeper: Sleeper,
    ) -> None:
        store = _make_store(tmp_path)
        plan = _plan(store)
        channel = ControlChannel(Program(ProgramState.initial(), policy))
        filler = Filler(_HoldingProducer(), channel, sleeper)
        channel.attach_reconciler(FillReconciler(filler, _FixedPlanSource(plan)))

        channel.post(TurnOn())  # empty pool -> generating_first -> filling True
        await channel.apply_next()
        assert filler.is_running  # the consumer started the fill

        channel.post(_turn_off(channel))  # -> off -> filling False
        await channel.apply_next()
        assert not filler.is_running  # the consumer cancelled the fill


@final
class _BoomProducer:
    """Raise an unexpected (non-Producer) error on every generation."""

    async def produce(self, spec: PartSpec, target: Path) -> Part:
        msg = "disk gone"
        raise OSError(msg)


@final
class _CountingHoldingProducer:
    """Count each generation, then block forever so a single call stays in flight."""

    __slots__ = ("calls",)
    calls: int

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self.calls = 0
        return self

    async def produce(self, spec: PartSpec, target: Path) -> Part:
        self.calls += 1
        await asyncio.Event().wait()  # never completes
        return Part(target.name, spec.index)


class TestTerminalStateCancelsFill:
    """Reaching a terminal, non-filling state cancels the fill so a Program
    that has given up makes no further provider calls -- no leftover window."""

    async def test_retry_exhausted_to_failed_cancels_the_fill(
        self,
        tmp_path: Path,
        policy: PlaybackPolicy,
        sleeper: Sleeper,
    ) -> None:
        store = _make_store(tmp_path)
        plan = _plan(store)
        # Drive a Program to retrying at the cap with an empty pool.
        prog = Program(ProgramState.initial(), policy)
        prog.turn_on()
        prog.first_track_transient(Reason("boom"))  # retrying, attempts 1, empty
        for _ in range(MAX_RETRY - 1):
            prog.retry_fails(Reason("boom"))
        assert prog.state.attempts == MAX_RETRY

        channel = ControlChannel(prog)
        producer = _CountingHoldingProducer()
        filler = Filler(producer, channel, sleeper)
        channel.attach_reconciler(FillReconciler(filler, _FixedPlanSource(plan)))
        filler.ensure_running(plan)  # model a fill still live as the Program gives up
        await _until(lambda: producer.calls == 1)  # one generation in flight, running

        # At the cap with an empty pool, one more transient exhausts -> failed.
        channel.post(TransientFailure(Reason("boom")))
        await channel.apply_next()
        assert prog.mode is Mode.FAILED
        assert (
            not filler.is_running
        )  # reconcile cancelled the fill on the terminal state

        # The single in-flight generation is uncancellable (its output is
        # discarded), but the cancelled loop starts NO new one: calls stay at 1.
        for _ in range(10):
            await asyncio.sleep(0)
        assert producer.calls == 1


class TestUnexpectedFillErrorIsObservable:
    """An unexpected fill error drives the Program to an observable
    failed state through the posted outcome, and the reconcile then cancels the
    dead-ended fill -- never a silent hang with filling still True."""

    async def test_generating_first_unexpected_error_fails_the_program(
        self,
        tmp_path: Path,
        policy: PlaybackPolicy,
        sleeper: Sleeper,
    ) -> None:
        store = _make_store(tmp_path)
        plan = _plan(store)
        channel = ControlChannel(Program(ProgramState.initial(), policy))
        filler = Filler(_BoomProducer(), channel, sleeper)
        channel.attach_reconciler(FillReconciler(filler, _FixedPlanSource(plan)))

        server = asyncio.create_task(channel.serve())
        channel.post(TurnOn())  # empty pool -> generating_first -> starts the fill
        await _reach(channel, Mode.FAILED)
        server.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server

        assert _prog(channel).mode is Mode.FAILED  # OBSERVABLE, not a silent hang
        assert not filler.is_running  # reconcile cancelled the dead-ended fill
        failed = [e for e in store.manifest().parts if not e.is_ready]
        assert failed and failed[0].reason is not None
        assert failed[0].reason.startswith("unexpected:")


async def _reach(channel: ControlChannel, mode: Mode, *, spins: int = 500) -> None:
    """Spin the event loop until the Program reaches ``mode`` (or give up)."""
    for _ in range(spins):
        if _prog(channel).mode is mode:
            return
        await asyncio.sleep(0)


async def _until(predicate: Callable[[], bool], *, spins: int = 50) -> None:
    """Spin the event loop until ``predicate`` holds (or give up)."""
    for _ in range(spins):
        if predicate():
            return
        await asyncio.sleep(0)


@final
@dataclass(frozen=True, slots=True)
class _Boom:
    """A control signal whose ``apply`` raises -- a bug, not a lost race."""

    error: Exception

    @property
    def interrupts(self) -> bool:
        return False

    def apply(self, source: PlaybackSource, /) -> None:
        raise self.error


class TestWriterSurvivesFailures:
    """A non-guard failure surfaces at ERROR, never masquerades as a race,
    and always sets ``changed`` so the playback loop can never block forever."""

    async def test_guard_violation_is_swallowed_and_logs_the_mode(
        self, policy: PlaybackPolicy, caplog: pytest.LogCaptureFixture
    ) -> None:
        channel = ControlChannel(Program(ProgramState.initial(), policy))
        with caplog.at_level(logging.INFO):
            channel.post(_Boom(GuardViolationError("rotate after off")))
            await channel.apply_next()  # a benign race -> does NOT raise
        assert channel.changed.is_set()
        messages = [r.getMessage() for r in caplog.records]
        assert any("lost race" in m and "off" in m for m in messages)  # mode logged

    async def test_plain_valueerror_is_not_swallowed_as_a_race(
        self, policy: PlaybackPolicy
    ) -> None:
        # A corrupt-successor ValueError is a bug, not a race -- it propagates
        # (to the serve guard), and is NOT logged as a lost race.
        channel = ControlChannel(Program(ProgramState.initial(), policy))
        channel.post(_Boom(ValueError("S13: corrupt successor")))
        with pytest.raises(ValueError, match="corrupt successor"):
            await channel.apply_next()
        assert channel.changed.is_set()  # still woke the loop

    async def test_unexpected_error_sets_changed_and_propagates(
        self, policy: PlaybackPolicy
    ) -> None:
        channel = ControlChannel(Program(ProgramState.initial(), policy))
        channel.post(_Boom(RuntimeError("kaboom")))
        with pytest.raises(RuntimeError, match="kaboom"):
            await channel.apply_next()
        assert channel.changed.is_set()  # the finally always wakes the loop

    async def test_serve_survives_a_crash_and_applies_the_next_command(
        self, policy: PlaybackPolicy, caplog: pytest.LogCaptureFixture
    ) -> None:
        channel = ControlChannel(Program(ProgramState.initial(), policy))
        server = asyncio.create_task(channel.serve())
        with caplog.at_level(logging.ERROR):
            channel.post(_Boom(RuntimeError("kaboom")))  # crashes the apply
            channel.post(TurnOn())  # the writer must still get here
            await channel.join()
        server.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server
        # The sole writer survived the crash and applied the next command.
        assert _prog(channel).mode is Mode.GENERATING_FIRST
        assert any(
            "unexpected error" in r.getMessage() and r.levelno == logging.ERROR
            for r in caplog.records
        )
