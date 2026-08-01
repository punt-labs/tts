"""Behavioral tests for the mpv playback loop.

Every test drives the REAL loop + the REAL ControlChannel consumer with a fake
:class:`Player` whose ended-futures and control calls the test observes.
Assertions are on what the loop actually *loaded* -- a different part on the end
of a part (auto-advance is a real, listened-to transition), the current part
replayed after a crash (I6), the load honouring the paused flag on a reload
(Fork B / I6), the ``eof``-before-pause guard (Z ``T3``), and the process
stopped on off -- never on removed internals. The crash tests assert the modeled
invariants by name: I7 (a crash resolves the loop's await -- no test hangs),
single-loadfile-ownership (the fake sees ``play`` only from the loop, never a
supervisor), and I6 (a crash while paused replays paused).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Self, cast, final

from punt_vox.types_programs import Format, Mode
from punt_vox.types_programs.mpv_event import EndFileReason
from punt_vox.types_programs.playback_fault import PlaybackFaultKind
from punt_vox.voxd.programs import Part, PlaybackPolicy, Program, ProgramState
from punt_vox.voxd.programs.active_context import ActiveContext
from punt_vox.voxd.programs.control_channel import ControlChannel
from punt_vox.voxd.programs.control_signal import ControlSignal
from punt_vox.voxd.programs.fill_signal import Produced
from punt_vox.voxd.programs.lifecycle_signal import TurnOff, TurnOn, VibeStyleChange
from punt_vox.voxd.programs.loop import ProgramLoop
from punt_vox.voxd.programs.playback_health import PlaybackHealth
from punt_vox.voxd.programs.playback_signal import Rotate, StepForward
from punt_vox.voxd.programs.suspension import PlaybackSuspension

from ._mpv_fakes import FakePlayer
from .conftest import AvoidRepeatPolicy, FakeSleeper

if TYPE_CHECKING:
    import pytest

PoolFactory = Callable[..., frozenset[Part]]
RotatingFactory = Callable[[PlaybackPolicy], Program]


def _prog(channel: ControlChannel) -> Program:
    """Return the channel's active source narrowed to the Program under test."""
    return cast("Program", channel.source)


def _turn_off(channel: ControlChannel) -> TurnOff:
    """Build a source-agnostic TurnOff (idle program used only for the replay path)."""
    idle = Program(ProgramState.initial(), AvoidRepeatPolicy())
    return TurnOff(channel, ActiveContext(), idle)


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def _stop(*tasks: asyncio.Task[None]) -> None:
    """Cancel and reap the loop + writer tasks of a harness."""
    for task in tasks:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@final
class _Harness:
    """A running channel consumer + loop over a fake mpv player, torn down together."""

    __slots__ = (
        "_loop",
        "_serve",
        "channel",
        "health",
        "player",
        "sleeper",
        "suspension",
    )
    channel: ControlChannel
    player: FakePlayer
    health: PlaybackHealth
    sleeper: FakeSleeper
    suspension: PlaybackSuspension
    _serve: asyncio.Task[None]
    _loop: asyncio.Task[None]

    def __new__(cls, program: Program) -> Self:
        self = super().__new__(cls)
        self.channel = ControlChannel(program)
        self.player = FakePlayer()
        self.health = PlaybackHealth()
        self.sleeper = FakeSleeper()
        self.suspension = PlaybackSuspension(self.player)
        loop = ProgramLoop(
            self.channel, self.player, self.sleeper, self.health, self.suspension
        )
        self._serve = asyncio.create_task(self.channel.serve())
        self._loop = asyncio.create_task(loop.run())
        return self

    async def stop(self) -> None:
        await _stop(self._loop, self._serve)


class TestAutoAdvance:
    async def test_part_end_advances_to_different_part(self, rotating: Program) -> None:
        harness = _Harness(rotating)
        await harness.player.wait_for(1)
        first = harness.player.parts[0]
        harness.player.handles[0].finish(EndFileReason.EOF)  # natural end -- no skip
        await harness.player.wait_for(2)
        assert harness.player.parts[1] != first  # auto-advanced to a different Part
        await harness.stop()

    async def test_full_pool_rotates_without_repeat(self, rotating: Program) -> None:
        harness = _Harness(rotating)
        await harness.player.wait_for(1)
        harness.player.handles[0].finish(EndFileReason.EOF)
        await harness.player.wait_for(2)
        harness.player.handles[1].finish(EndFileReason.EOF)
        await harness.player.wait_for(3)
        assert harness.player.parts[0] != harness.player.parts[1]
        assert harness.player.parts[1] != harness.player.parts[2]
        await harness.stop()


class TestSingleTrackLoops:
    async def test_part_end_replays_sole_part(self, policy: PlaybackPolicy) -> None:
        prog = Program(ProgramState.initial(), policy)
        prog.turn_on()
        prog.first_track_ok(Part("id001", 1))  # playing_filling, pool of one
        harness = _Harness(prog)
        await harness.player.wait_for(1)
        harness.player.handles[0].finish(EndFileReason.EOF)
        await harness.player.wait_for(2)
        assert harness.player.parts[1] == harness.player.parts[0]  # looped the one Part
        await harness.stop()


class TestRetuneFinishesThenSwitches:
    async def test_current_survives_then_switches_pool(
        self, rotating: Program, pool_of: PoolFactory
    ) -> None:
        harness = _Harness(rotating)
        await harness.player.wait_for(1)
        harness.channel.post(VibeStyleChange(pool_of(20, 21)))  # retune (no interrupt)
        await _settle()
        assert (
            len(harness.player.parts) == 1
        )  # current NOT interrupted -- still awaited
        harness.player.handles[0].finish(EndFileReason.EOF)  # current finishes
        await harness.player.wait_for(2)
        assert harness.player.parts[1].index in {20, 21}  # switched to the new pool
        await harness.stop()


class TestOffInterrupts:
    async def test_off_stops_the_player_and_idles(self, rotating: Program) -> None:
        harness = _Harness(rotating)
        await harness.player.wait_for(1)
        harness.channel.post(_turn_off(harness.channel))  # interrupts
        await _settle()
        assert harness.player.stops >= 1  # mpv unloaded (stop), not advanced
        assert _prog(harness.channel).mode is Mode.OFF
        await harness.stop()


class TestSkipInterrupts:
    async def test_skip_plays_next(self, rotating: Program) -> None:
        harness = _Harness(rotating)
        await harness.player.wait_for(1)
        first = harness.player.parts[0]
        harness.channel.post(Rotate())  # a user skip interrupts
        await harness.player.wait_for(2)
        assert harness.player.parts[1] != first  # loaded the advanced part
        await harness.stop()


class TestGeneratingFirstThenPlays:
    async def test_empty_pool_awaits_then_plays_first_part(
        self, policy: PlaybackPolicy
    ) -> None:
        harness = _Harness(Program(ProgramState.initial(), policy))
        harness.channel.post(TurnOn())  # empty pool -> generating_first
        await _settle()
        assert harness.player.parts == []  # nothing plays before the first part
        assert _prog(harness.channel).mode is Mode.GENERATING_FIRST
        harness.channel.post(Produced(Part("id001", 1)))  # the fill delivers #1
        await harness.player.wait_for(1)
        assert harness.player.parts[0] == Part("id001", 1)
        await harness.stop()


class TestSkipInGeneratingFirst:
    """A skip while nothing plays is a no-op (the modeled empty-pool property)."""

    async def test_skip_in_generating_first_is_noop(
        self, policy: PlaybackPolicy
    ) -> None:
        harness = _Harness(Program(ProgramState.initial(), policy))
        harness.channel.post(TurnOn())  # empty pool -> generating_first
        await _settle()
        assert _prog(harness.channel).mode is Mode.GENERATING_FIRST

        harness.channel.post(Rotate())  # a skip here has no playing Part to advance
        await _settle()
        assert harness.player.parts == []  # NO load issued
        assert _prog(harness.channel).mode is Mode.GENERATING_FIRST
        await harness.stop()


class TestRetuneDuringGeneratingFirst:
    """Retuning to a full pool from generating_first wakes the loop and plays."""

    async def test_retune_during_generating_first_never_hangs(
        self, policy: PlaybackPolicy, pool_of: PoolFactory
    ) -> None:
        harness = _Harness(Program(ProgramState.initial(), policy))
        harness.channel.post(TurnOn())  # generating_first, parked in _wait_for_playable
        await _settle()
        assert _prog(harness.channel).mode is Mode.GENERATING_FIRST
        assert harness.player.parts == []

        full = pool_of(*range(1, Format.PLAYLIST.pool_size + 1))
        harness.channel.post(VibeStyleChange(full))  # full pool -> playing_rotating
        await harness.player.wait_for(1)
        assert _prog(harness.channel).mode is Mode.PLAYING_ROTATING
        assert harness.player.parts[0] in full
        await harness.stop()


class TestConcurrentControlsStaySequential:
    """O2: concurrent skip + retune + a fill completion never corrupt the loop."""

    async def test_concurrent_next_vibe_and_fill(
        self,
        make_rotating: RotatingFactory,
        policy: PlaybackPolicy,
        pool_of: PoolFactory,
    ) -> None:
        harness = _Harness(make_rotating(policy))
        await harness.player.wait_for(1)
        await asyncio.gather(
            _post(harness.channel, Rotate()),
            _post(harness.channel, VibeStyleChange(pool_of(20, 21))),
            _post(harness.channel, Produced(Part("id020", 20))),
        )
        await harness.channel.join()
        prog = _prog(harness.channel)
        assert {p.index for p in prog.pool} <= {20, 21}
        assert prog.mode in {Mode.PLAYING_FILLING, Mode.PLAYING_ROTATING}
        await harness.stop()


async def _post(channel: ControlChannel, signal: ControlSignal) -> None:
    await asyncio.sleep(0)
    channel.post(signal)


class TestPauseResume:
    """Pause holds the current load in place (no reload); resume continues it."""

    async def test_pause_does_not_advance_and_resume_continues(
        self, rotating: Program
    ) -> None:
        harness = _Harness(rotating)
        await harness.player.wait_for(1)
        first = harness.player.parts[0]

        harness.suspension.pause()  # click-free set_property pause; loop keeps awaiting
        await _settle()
        assert harness.suspension.is_paused
        assert harness.player.pauses == 1  # delegated to the player
        assert len(harness.player.parts) == 1  # NO reload -- the click-free hold

        harness.suspension.resume()
        await _settle()
        assert harness.player.resumes == 1
        assert (
            len(harness.player.parts) == 1
        )  # still no reload -- mpv continued in place

        harness.player.handles[0].finish(
            EndFileReason.EOF
        )  # the eof arrives after resume
        await harness.player.wait_for(2)
        assert harness.player.parts[1] != first  # advanced once resumed
        await harness.stop()

    async def test_next_while_paused_plays_new_part_paused(
        self, rotating: Program
    ) -> None:
        harness = _Harness(rotating)
        await harness.player.wait_for(1)
        first = harness.player.parts[0]
        harness.suspension.pause()
        await _settle()

        harness.channel.post(StepForward())  # move the cursor while paused (interrupts)
        await harness.player.wait_for(2)
        assert harness.player.parts[1] != first  # played the newly-cursored part
        assert harness.player.paused_flags[1] is True  # loaded paused (Fork B / I6)
        await harness.stop()


class TestPausedEofGuard:
    """Z T3: an eof buffered just before a pause must not advance the cursor."""

    async def test_eof_under_pause_reloads_current_paused(
        self, rotating: Program
    ) -> None:
        harness = _Harness(rotating)
        await harness.player.wait_for(1)
        first = harness.player.parts[0]

        harness.suspension.pause()
        await _settle()
        # An eof mpv buffered in the instant before the pause resolves the load.
        harness.player.handles[0].finish(EndFileReason.EOF)
        await harness.player.wait_for(2)

        assert (
            harness.player.parts[1] == first
        )  # T3: did NOT advance -- current reloaded
        assert harness.player.paused_flags[1] is True  # reloaded paused (I6)
        await harness.stop()


class TestCrashRecovery:
    """A crash replays the current part after WaitReady, honouring pause (I6/I7)."""

    async def test_crash_mid_playback_replays_current(self, rotating: Program) -> None:
        harness = _Harness(rotating)
        await harness.player.wait_for(1)
        first = harness.player.parts[0]

        harness.player.become_not_ready()  # the reconnect gap after the crash
        harness.player.handles[0].crash()  # socket EOF -> synthetic ``crashed`` (I7)
        await _settle()
        assert len(harness.player.parts) == 1  # parked in WaitReady -- no advance

        harness.player.become_ready()  # mpv reconnected
        await harness.player.wait_for(2)
        assert harness.player.parts[1] == first  # replayed the CURRENT part (I6)
        assert harness.player.paused_flags[1] is False  # not paused -> plays
        await harness.stop()

    async def test_crash_while_paused_replays_paused(self, rotating: Program) -> None:
        harness = _Harness(rotating)
        await harness.player.wait_for(1)
        first = harness.player.parts[0]

        harness.suspension.pause()
        await _settle()
        harness.player.become_not_ready()
        harness.player.handles[0].crash()
        await _settle()

        harness.player.become_ready()
        await harness.player.wait_for(2)
        assert harness.player.parts[1] == first  # cursor unmoved (I6)
        assert harness.player.paused_flags[1] is True  # reload honours pause (I6)
        await harness.stop()


@final
class _GateSleeper:
    """A Sleeper whose ``sleep`` blocks until released -- pins the backoff window."""

    __slots__ = ("_gate", "sleeps")
    _gate: asyncio.Event
    sleeps: list[float]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._gate = asyncio.Event()
        self.sleeps = []
        return self

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await self._gate.wait()

    def release(self) -> None:
        self._gate.set()


class TestLoadFailure:
    """A load mpv will not accept is observable and bounded, never a hot spin."""

    async def test_load_failure_is_observable_and_backs_off(
        self, rotating: Program, caplog: pytest.LogCaptureFixture
    ) -> None:
        channel = ControlChannel(rotating)
        player = FakePlayer()
        player.fail_next_load(ConnectionError("mpv is not ready"))
        health = PlaybackHealth()
        sleeper = _GateSleeper()
        suspension = PlaybackSuspension(player)
        loop = ProgramLoop(channel, player, sleeper, health, suspension)
        serve = asyncio.create_task(channel.serve())
        with caplog.at_level(logging.ERROR):
            run = asyncio.create_task(loop.run())
            for _ in range(500):
                if health.fault is not None and sleeper.sleeps:
                    break
                await asyncio.sleep(0)

            fault = health.fault
            assert fault is not None  # observable via status, not swallowed
            assert fault.kind is PlaybackFaultKind.PLAYER_UNAVAILABLE
            assert sleeper.sleeps  # backed off rather than spinning hot

            sleeper.release()  # let the loop retry; the next load succeeds
            await player.wait_for(1)
            assert player.parts  # recovered and loaded after the failed load
        await _stop(run, serve)

    async def test_rejected_loadfile_surfaces_player_unavailable(
        self, rotating: Program
    ) -> None:
        # A command-level loadfile rejection reaches the loop as ConnectionError
        # (raised by MpvProgramPlayer.play); the loop must surface it on the
        # health fault, not park silently on a never-resolving ended-future.
        channel = ControlChannel(rotating)
        player = FakePlayer()
        player.fail_next_load(ConnectionError("mpv rejected loadfile: unknown command"))
        health = PlaybackHealth()
        sleeper = _GateSleeper()
        loop = ProgramLoop(channel, player, sleeper, health, PlaybackSuspension(player))
        serve = asyncio.create_task(channel.serve())
        run = asyncio.create_task(loop.run())
        for _ in range(500):
            if health.fault is not None and sleeper.sleeps:
                break
            await asyncio.sleep(0)

        fault = health.fault
        assert fault is not None  # observable via status, not a silent wedge
        assert fault.kind is PlaybackFaultKind.PLAYER_UNAVAILABLE
        assert "mpv rejected loadfile" in fault.reason
        await _stop(run, serve)


class TestErrorReasonAdvances:
    """A bad-file ``error`` reason records a per-part fault, then advances past it."""

    async def test_error_records_a_fault_and_advances(self, rotating: Program) -> None:
        channel = ControlChannel(rotating)
        player = FakePlayer()
        health = PlaybackHealth()
        sleeper = _GateSleeper()
        suspension = PlaybackSuspension(player)
        loop = ProgramLoop(channel, player, sleeper, health, suspension)
        serve = asyncio.create_task(channel.serve())
        run = asyncio.create_task(loop.run())

        await player.wait_for(1)
        first = player.parts[0]
        player.handles[0].finish(EndFileReason.ERROR)  # mpv could not play the file
        for _ in range(500):
            if health.fault is not None and sleeper.sleeps:
                break
            await asyncio.sleep(0)

        fault = health.fault
        assert fault is not None
        assert fault.kind is PlaybackFaultKind.TRACK_ERROR
        assert fault.part_index == first.index

        sleeper.release()  # let the loop advance past the bad part
        await player.wait_for(2)
        assert player.parts[1] != first  # skipped forward
        await _stop(run, serve)


class TestLoopSurvivesAFailingStep:
    """The run() guard keeps playback alive when one step raises unexpectedly."""

    async def test_run_survives_and_plays_after_a_failing_step(
        self, rotating: Program, caplog: pytest.LogCaptureFixture
    ) -> None:
        channel = ControlChannel(rotating)
        player = FakePlayer()
        player.fail_next_load(RuntimeError("boom"))  # an unexpected error, not IO
        loop = ProgramLoop(
            channel, player, FakeSleeper(), PlaybackHealth(), PlaybackSuspension(player)
        )
        serve = asyncio.create_task(channel.serve())
        run = asyncio.create_task(loop.run())
        with caplog.at_level(logging.ERROR):
            for _ in range(500):
                if player.parts:
                    break
                await asyncio.sleep(0)
        await _stop(run, serve)
        assert player.parts  # the loop recovered and played after the crash
        assert any(
            "unexpected error in a step" in r.getMessage() for r in caplog.records
        )
