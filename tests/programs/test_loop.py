"""Behavioral-parity snapshot for the playback loop (ports tests/music/test_loop.py).

Every test drives the REAL loop + the REAL ControlChannel consumer with a fake
player whose process end the test controls. Assertions are on what the loop
actually *spawned* -- a different file on track-end (the auto-advance is a real,
listened-to transition), no advance past a retune's finish, the current player
surviving a retune, the player killed on off/skip -- and on the Program mode,
never on removed scheduler internals.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Self, cast, final

from punt_vox.types_programs import Format, Mode
from punt_vox.voxd.programs import Part, PlaybackPolicy, Program, ProgramState
from punt_vox.voxd.programs.active_context import ActiveContext
from punt_vox.voxd.programs.control_channel import ControlChannel
from punt_vox.voxd.programs.control_signal import ControlSignal
from punt_vox.voxd.programs.fill_signal import Produced
from punt_vox.voxd.programs.lifecycle_signal import TurnOff, TurnOn, VibeStyleChange
from punt_vox.voxd.programs.loop import ProgramLoop
from punt_vox.voxd.programs.playback_health import PlaybackHealth
from punt_vox.voxd.programs.playback_signal import Rotate

from .conftest import AvoidRepeatPolicy, FakeSleeper

if TYPE_CHECKING:
    from pathlib import Path

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


@final
class FakeProcess:
    """A fake player process whose end the test controls."""

    __slots__ = ("_ended", "rc")
    rc: int | None
    _ended: asyncio.Event

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self.rc = None
        self._ended = asyncio.Event()
        return self

    async def wait(self) -> int:
        await self._ended.wait()
        return self.rc if self.rc is not None else 0

    async def kill(self) -> None:
        self.rc = -9
        self._ended.set()

    def end(self, rc: int = 0) -> None:
        """Signal a natural end (test control)."""
        self.rc = rc
        self._ended.set()


@final
class FakePlayer:
    """Record the Parts played and hand back controllable processes."""

    __slots__ = ("_spawned", "parts", "procs")
    parts: list[Part]
    procs: list[FakeProcess]
    _spawned: asyncio.Event

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self.parts = []
        self.procs = []
        self._spawned = asyncio.Event()
        return self

    async def play(self, part: Part) -> FakeProcess:
        self.parts.append(part)
        proc = FakeProcess()
        self.procs.append(proc)
        self._spawned.set()
        return proc

    async def wait_for(self, count: int) -> None:
        while len(self.parts) < count:
            self._spawned.clear()
            if len(self.parts) >= count:
                return
            await asyncio.wait_for(self._spawned.wait(), timeout=2.0)


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


class _Harness:
    """A running channel consumer + loop, torn down together."""

    __slots__ = ("_loop", "_serve", "channel", "health", "player", "sleeper")
    channel: ControlChannel
    player: FakePlayer
    health: PlaybackHealth
    sleeper: FakeSleeper
    _serve: asyncio.Task[None]
    _loop: asyncio.Task[None]

    def __new__(cls, program: Program) -> Self:
        self = super().__new__(cls)
        self.channel = ControlChannel(program)
        self.player = FakePlayer()
        self.health = PlaybackHealth()
        self.sleeper = FakeSleeper()
        loop = ProgramLoop(self.channel, self.player, self.sleeper, self.health)
        self._serve = asyncio.create_task(self.channel.serve())
        self._loop = asyncio.create_task(loop.run())
        return self

    async def stop(self) -> None:
        for task in (self._loop, self._serve):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


class TestAutoAdvance:
    async def test_track_end_advances_to_different_track(
        self, rotating: Program
    ) -> None:
        harness = _Harness(rotating)
        await harness.player.wait_for(1)
        first = harness.player.parts[0]
        harness.player.procs[0].end()  # natural end -- no skip
        await harness.player.wait_for(2)
        assert harness.player.parts[1] != first  # auto-advanced to a different Part
        await harness.stop()

    async def test_full_pool_rotates_without_repeat(self, rotating: Program) -> None:
        harness = _Harness(rotating)
        await harness.player.wait_for(1)
        harness.player.procs[0].end()
        await harness.player.wait_for(2)
        harness.player.procs[1].end()
        await harness.player.wait_for(3)
        assert harness.player.parts[0] != harness.player.parts[1]
        assert harness.player.parts[1] != harness.player.parts[2]
        await harness.stop()


class TestSingleTrackLoops:
    async def test_track_end_replays_sole_part(self, policy: PlaybackPolicy) -> None:
        prog = Program(ProgramState.initial(), policy)
        prog.turn_on()
        prog.first_track_ok(Part("id001", 1))  # playing_filling, pool of one
        harness = _Harness(prog)
        await harness.player.wait_for(1)
        harness.player.procs[0].end()
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
        assert harness.player.procs[0].rc is None  # current NOT killed
        harness.player.procs[0].end()  # current finishes
        await harness.player.wait_for(2)
        assert harness.player.parts[1].index in {20, 21}  # switched to the new pool
        await harness.stop()


class TestOffInterrupts:
    async def test_off_kills_current_and_stops(self, rotating: Program) -> None:
        harness = _Harness(rotating)
        await harness.player.wait_for(1)
        harness.channel.post(_turn_off(harness.channel))  # interrupts
        await _settle()
        assert harness.player.procs[0].rc == -9  # player killed, not ended
        assert _prog(harness.channel).mode is Mode.OFF
        await harness.stop()


class TestSkipInterrupts:
    async def test_skip_kills_current_and_plays_next(self, rotating: Program) -> None:
        harness = _Harness(rotating)
        await harness.player.wait_for(1)
        first = harness.player.parts[0]
        harness.channel.post(Rotate())  # a user skip interrupts
        await harness.player.wait_for(2)
        assert harness.player.procs[0].rc == -9  # killed mid-track
        assert harness.player.parts[1] != first
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


class TestExitFault:
    """A non-zero player exit is observable via status, not a silent skip.

    The fault clears on the *next* successful spawn (the ``PlaybackHealth``
    contract), so the test pins the loop in the exit-fault backoff with a gated
    sleeper to observe the standing fault before the loop advances and clears it.
    """

    async def test_non_zero_exit_records_a_fault_and_advances(
        self, rotating: Program
    ) -> None:
        health = PlaybackHealth()
        sleeper = _GateSleeper()
        player = FakePlayer()
        channel = ControlChannel(rotating)
        loop = ProgramLoop(channel, player, sleeper, health)
        serve = asyncio.create_task(channel.serve())
        run = asyncio.create_task(loop.run())

        await player.wait_for(1)
        first = player.parts[0]
        player.procs[0].end(rc=1)  # a corrupt/missing track exits non-zero
        for _ in range(500):  # let the loop record the fault and park in the backoff
            if health.fault is not None and sleeper.sleeps:
                break
            await asyncio.sleep(0)

        fault = health.fault
        assert fault is not None  # surfaced on the status health surface, not swallowed
        assert fault.part_index == first.index
        assert "code 1" in fault.reason
        assert sleeper.sleeps  # backed off so a corrupt pool cannot spin hot

        sleeper.release()  # let the loop advance past the bad track
        await player.wait_for(2)
        assert player.parts[1] != first  # skipped forward, radio kept playing
        for task in (run, serve):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


class TestGeneratingFirstThenPlays:
    async def test_empty_pool_awaits_then_plays_first_track(
        self, policy: PlaybackPolicy
    ) -> None:
        harness = _Harness(Program(ProgramState.initial(), policy))
        harness.channel.post(TurnOn())  # empty pool -> generating_first
        await _settle()
        assert harness.player.parts == []  # nothing plays before the first track
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
        harness.channel.post(
            TurnOn()
        )  # empty pool -> generating_first, nothing playing
        await _settle()
        assert _prog(harness.channel).mode is Mode.GENERATING_FIRST

        harness.channel.post(Rotate())  # a skip here has no playing Part to advance
        await _settle()
        assert harness.player.parts == []  # NO player spawned
        # The lost-race guard is swallowed: the mode is unchanged and nothing crashed.
        assert _prog(harness.channel).mode is Mode.GENERATING_FIRST
        await harness.stop()


class TestRetuneDuringGeneratingFirst:
    """Modeled property: retuning to a full pool from generating_first wakes the
    loop and plays from disk immediately -- it never hangs waiting for a fill."""

    async def test_retune_during_generating_first_never_hangs(
        self, policy: PlaybackPolicy, pool_of: PoolFactory
    ) -> None:
        harness = _Harness(Program(ProgramState.initial(), policy))
        harness.channel.post(TurnOn())  # generating_first, parked in _wait_for_playable
        await _settle()
        mode_before = _prog(harness.channel).mode
        assert mode_before is Mode.GENERATING_FIRST
        assert harness.player.parts == []  # nothing playing yet

        full = pool_of(*range(1, Format.PLAYLIST.pool_size + 1))
        harness.channel.post(VibeStyleChange(full))  # full pool -> playing_rotating
        # The loop wakes on `changed` and plays a saved Part at once -- no hang.
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
        # Three clients fire at once through the single writer.
        await asyncio.gather(
            _post(harness.channel, Rotate()),
            _post(harness.channel, VibeStyleChange(pool_of(20, 21))),
            _post(harness.channel, Produced(Part("id020", 20))),
        )
        await harness.channel.join()
        prog = _prog(harness.channel)
        # The Program is always a legal state; the last retune won the pool.
        assert {p.index for p in prog.pool} <= {20, 21}
        assert prog.mode in {Mode.PLAYING_FILLING, Mode.PLAYING_ROTATING}
        await harness.stop()


async def _post(channel: ControlChannel, signal: ControlSignal) -> None:
    await asyncio.sleep(0)
    channel.post(signal)


@final
class _RaisingProcess:
    """A process whose wait() raises -- a player error, not a clean track end."""

    __slots__ = ("rc",)
    rc: int | None

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self.rc = None
        return self

    async def wait(self) -> int:
        msg = "transport gone"
        raise RuntimeError(msg)

    async def kill(self) -> None:
        self.rc = -9


@final
class _ErrThenBlockPlayer:
    """Hand out a wait-raising process first, then a process that never ends."""

    __slots__ = ("parts", "procs")
    parts: list[Part]
    procs: list[_RaisingProcess | FakeProcess]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self.parts = []
        self.procs = []
        return self

    async def play(self, part: Part) -> _RaisingProcess | FakeProcess:
        self.parts.append(part)
        proc: _RaisingProcess | FakeProcess = (
            _RaisingProcess() if len(self.parts) == 1 else FakeProcess()
        )
        self.procs.append(proc)
        return proc


class TestPlayerWaitError:
    """A raised player wait() is a player error, never a clean advance.

    The unit-level ``_player_errored`` assertions moved to ``test_interrupt_race``
    with the extracted :class:`InterruptRace`; this end-to-end test proves the loop
    still routes a raised ``wait`` through the race to a kill-and-replay.
    """

    async def test_wait_error_kills_and_replays_without_advancing(
        self, rotating: Program, caplog: pytest.LogCaptureFixture
    ) -> None:
        player = _ErrThenBlockPlayer()
        channel = ControlChannel(rotating)
        loop = ProgramLoop(channel, player, FakeSleeper(), PlaybackHealth())
        serve = asyncio.create_task(channel.serve())
        run = asyncio.create_task(loop.run())
        with caplog.at_level(logging.ERROR):
            for _ in range(500):
                if len(player.parts) >= 2:
                    break
                await asyncio.sleep(0)
        for task in (run, serve):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert player.procs[0].rc == -9  # the errored player was killed, not ended
        assert (
            player.parts[1] == player.parts[0]
        )  # replayed, NOT advanced (no masquerade)
        assert any("player wait failed" in r.getMessage() for r in caplog.records)


@final
class _FailFirstPlayer:
    """Raise on the first play, then hand back a controllable process."""

    __slots__ = ("_calls", "parts", "procs")
    _calls: int
    parts: list[Part]
    procs: list[FakeProcess]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._calls = 0
        self.parts = []
        self.procs = []
        return self

    async def play(self, part: Part) -> FakeProcess:
        self._calls += 1
        if self._calls == 1:
            msg = "boom"
            raise RuntimeError(msg)
        self.parts.append(part)
        proc = FakeProcess()
        self.procs.append(proc)
        return proc


class TestLoopSurvivesAFailingStep:
    """The run() guard keeps playback alive when one step raises unexpectedly."""

    async def test_run_survives_and_plays_after_a_failing_step(
        self, rotating: Program, caplog: pytest.LogCaptureFixture
    ) -> None:
        player = _FailFirstPlayer()
        channel = ControlChannel(rotating)
        loop = ProgramLoop(channel, player, FakeSleeper(), PlaybackHealth())
        serve = asyncio.create_task(channel.serve())
        run = asyncio.create_task(loop.run())
        with caplog.at_level(logging.ERROR):
            for _ in range(500):
                if player.parts:
                    break
                await asyncio.sleep(0)
        for task in (run, serve):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert player.parts  # the loop recovered and played after the crash
        assert any(
            "unexpected error in a step" in r.getMessage() for r in caplog.records
        )


@final
class _AlwaysFailSpawnPlayer:
    """Raise ``FileNotFoundError`` on every spawn -- a missing afplay/ffplay."""

    __slots__ = ("parts",)
    parts: list[Part]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self.parts = []
        return self

    async def play(self, part: Part) -> FakeProcess:
        msg = "afplay: No such file or directory"
        raise FileNotFoundError(msg)


@final
class _FailSpawnOncePlayer:
    """Raise ``OSError`` on the first spawn, then hand back a live process."""

    __slots__ = ("_calls", "parts", "procs")
    _calls: int
    parts: list[Part]
    procs: list[FakeProcess]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._calls = 0
        self.parts = []
        self.procs = []
        return self

    async def play(self, part: Part) -> FakeProcess:
        self._calls += 1
        if self._calls == 1:
            msg = "EMFILE: too many open files"
            raise OSError(msg)
        self.parts.append(part)
        proc = FakeProcess()
        self.procs.append(proc)
        return proc


class TestSpawnFailure:
    """Fix #2: a player spawn failure is observable and bounded, never a hot spin."""

    async def test_spawn_failure_is_observable_and_backs_off(
        self, rotating: Program, caplog: pytest.LogCaptureFixture
    ) -> None:
        health = PlaybackHealth()
        sleeper = FakeSleeper()
        player = _AlwaysFailSpawnPlayer()
        channel = ControlChannel(rotating)
        loop = ProgramLoop(channel, player, sleeper, health)
        with caplog.at_level(logging.ERROR):
            run = asyncio.create_task(loop.run())
            for _ in range(200):
                if health.fault is not None and sleeper.sleeps:
                    break
                await asyncio.sleep(0)
            run.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run
        assert player.parts == []  # nothing became audible
        fault = health.fault
        assert fault is not None  # the failure is observable via status
        playing = rotating.playing
        assert playing is not None
        assert fault.part_index == playing.index
        assert "No such file" in fault.reason
        assert sleeper.sleeps  # it backed off rather than spinning hot
        assert any("player spawn failed" in r.getMessage() for r in caplog.records)

    async def test_spawn_recovers_and_clears_health(self, rotating: Program) -> None:
        health = PlaybackHealth()
        sleeper = FakeSleeper()
        player = _FailSpawnOncePlayer()
        channel = ControlChannel(rotating)
        loop = ProgramLoop(channel, player, sleeper, health)
        run = asyncio.create_task(loop.run())
        for _ in range(500):
            if player.parts:  # a spawn finally succeeded
                break
            await asyncio.sleep(0)
        run.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run
        assert player.parts  # recovered and played after the failed spawn
        assert health.fault is None  # the successful spawn cleared the fault
        assert sleeper.sleeps  # it backed off before recovering


@final
class _PathLeakSpawnPlayer:
    """Raise a spawn error whose message embeds absolute host paths."""

    __slots__ = ("_msg", "parts")
    _msg: str
    parts: list[Part]

    def __new__(cls, msg: str) -> Self:
        self = super().__new__(cls)
        self._msg = msg
        self.parts = []
        return self

    async def play(self, part: Part) -> FakeProcess:
        raise FileNotFoundError(self._msg)


class TestSpawnFaultReasonSanitization:
    """The status-surfaced spawn-fault reason carries no absolute prefix (#10/D4)."""

    async def test_spawn_fault_reason_relativizes_and_strips_paths(
        self, rotating: Program, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from punt_vox import paths

        state = (tmp_path / "state").resolve()
        (state / "recordings").mkdir(parents=True)
        monkeypatch.setattr(paths, "user_state_dir", lambda: state)
        rec = state / "recordings" / "t.mp3"
        player = _PathLeakSpawnPlayer(f"spawn {rec}: /opt/homebrew/bin/ffplay missing")
        health = PlaybackHealth()
        sleeper = FakeSleeper()
        channel = ControlChannel(rotating)
        loop = ProgramLoop(channel, player, sleeper, health)
        run = asyncio.create_task(loop.run())
        for _ in range(500):
            if health.fault is not None and sleeper.sleeps:
                break
            await asyncio.sleep(0)
        run.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run

        fault = health.fault
        assert fault is not None
        assert "recordings/t.mp3" in fault.reason  # in-jail token relativized
        assert str(state) not in fault.reason  # no absolute prefix
        assert "/opt/homebrew/bin/ffplay" not in fault.reason  # out-of-jail stripped
        assert "<path>" in fault.reason
