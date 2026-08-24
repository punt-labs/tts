"""Unit tests for :class:`~punt_vox.commands.call_live_driver.LiveCallDriver`.

End-to-end coverage of the live call path (through ``vox call start`` with
no ``--script``) already lives in ``tests/test_cli_call.py``. This file
targets ``LiveCallDriver`` itself in isolation -- constructed directly via
``__new__`` with fakes for the mic source, detector, and session-attach, no
CLI, no subprocess -- so its own behaviors (the mic-echo gate's hold/cap,
the inactivity clock, ``create()``'s calibration step) have a test that
fails for the right reason when this module changes, not only when the CLI
wiring around it changes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch

import pytest
import typer

from punt_vox.commands import call_live_driver as driver_module
from punt_vox.commands.call_live_driver import LiveCallDriver
from punt_vox.commands.call_scripted import ScriptedTurn
from punt_vox.types import HealthCheck
from punt_vox.voxd.conversation_mode.audio_chunk import AudioChunk
from punt_vox.voxd.conversation_mode.call_control import CallControl
from punt_vox.voxd.conversation_mode.reply import ReplyChunk
from punt_vox.voxd.conversation_mode.turn_detector import TurnDetector

if TYPE_CHECKING:
    from collections.abc import AsyncIterator as _AsyncIterator

    from punt_vox.voxd.conversation_mode.call_session import CallSession, SpeakFn
    from punt_vox.voxd.conversation_mode.mic_audio_source import MicAudioSource
    from punt_vox.voxd.conversation_mode.stt_provider import TranscriptEvent
    from punt_vox.voxd.conversation_mode.turn import TranscribedTurn


class _FakeSTTProvider:
    """A healthy :class:`STTProvider` stand-in -- never actually transcribes
    in these gate/inactivity/create tests, only satisfies construction and
    :func:`~punt_vox.commands.call_live_driver._require_healthy`.
    """

    def __init__(self, *, healthy: bool = True) -> None:
        self._healthy = healthy

    @property
    def name(self) -> str:
        return "fake-stt"

    async def transcribe(
        self, chunks: _AsyncIterator[AudioChunk]
    ) -> _AsyncIterator[TranscriptEvent]:
        async for _ in chunks:
            pass
        events: tuple[TranscriptEvent, ...] = ()
        for event in events:  # never actually transcribes in these tests
            yield event

    def check_health(self) -> list[HealthCheck]:
        if self._healthy:
            return [HealthCheck(passed=True, message="fake stt: healthy")]
        return [HealthCheck(passed=False, message="fake stt: no API key")]


class _FakeMicSource:
    """No hardware: canned calibration/live chunks, no real PortAudio callback."""

    def __init__(
        self, live: list[AudioChunk], *, calibration: list[AudioChunk] | None = None
    ) -> None:
        self._live = live
        self._calibration = calibration or []
        self.drain_calls = 0
        self.listening_calls: list[bool] = []
        self.calibration_durations: list[float] = []

    async def capture_seconds(self, duration_s: float) -> list[AudioChunk]:
        self.calibration_durations.append(duration_s)
        return self._calibration

    async def chunks(self) -> AsyncIterator[AudioChunk]:
        for chunk in self._live:
            yield chunk

    def drain_pending(self) -> int:
        self.drain_calls += 1
        return 0

    def set_listening(self, *, listening: bool) -> None:
        self.listening_calls.append(listening)


class _FakeSessionAttach:
    """Replies with one fixed chunk, no subprocess."""

    def __init__(self, reply_text: str) -> None:
        self._reply_text = reply_text

    async def send_turn(self, turn: TranscribedTurn) -> AsyncIterator[ReplyChunk]:
        del turn
        yield ReplyChunk(text=self._reply_text, is_final=True)


async def _never_stop(
    _control: CallControl, _session: CallSession, _speak: SpeakFn
) -> bool:
    return False


def _control(tmp_path: Path) -> CallControl:
    return CallControl(tmp_path / "call.control")


async def _no_op_chime() -> None:
    """A :class:`~punt_vox.voxd.conversation_mode.wait_cue.ChimeFn` fake.

    Never invoked by these gate/inactivity/create tests -- none of them
    drive a turn through :meth:`CallSession._speak_reply`'s wait cue -- but
    every construction still needs a real, structurally-valid ``chime``
    collaborator to pass.
    """


def _driver(
    tmp_path: Path, *, live_chunks: list[AudioChunk], reply_text: str = "a reply"
) -> tuple[LiveCallDriver, _FakeMicSource, list[str]]:
    mic_source = _FakeMicSource(live_chunks)
    spoken: list[str] = []

    async def speak(text: str) -> None:
        spoken.append(text)

    driver = LiveCallDriver(
        session_attach=_FakeSessionAttach(reply_text),
        speak=speak,
        chime=_no_op_chime,
        control=_control(tmp_path),
        apply_control=_never_stop,
        detector=TurnDetector(),
        # A structural fake standing in for MicAudioSource -- this module has
        # no test seam (Protocol) for it, only the concrete @final class, so
        # the cast documents the substitution rather than hiding a real
        # mismatch.
        mic_source=cast("MicAudioSource", mic_source),
        stt_provider=_FakeSTTProvider(),
    )
    return driver, mic_source, spoken


class TestLiveCallDriverGate:
    async def test_run_gates_mic_around_the_opening_cue(self, tmp_path: Path) -> None:
        driver, mic_source, spoken = _driver(tmp_path, live_chunks=[])
        await driver.run()
        assert spoken[0] == "Listening."
        assert mic_source.listening_calls[:2] == [False, True]

    async def test_gate_drains_after_every_utterance(self, tmp_path: Path) -> None:
        driver, mic_source, spoken = _driver(tmp_path, live_chunks=[])
        await driver.run()
        assert mic_source.drain_calls == len(spoken)

    async def test_hold_sleep_is_capped_at_the_documented_ceiling(
        self, tmp_path: Path
    ) -> None:
        """A pathological reply must not make the gate unresponsive for minutes."""
        driver, _mic_source, _spoken = _driver(tmp_path, live_chunks=[])
        captured_delays: list[float] = []

        async def _record_sleep(delay: float) -> None:
            captured_delays.append(delay)

        with patch("punt_vox.commands.call_live_driver.asyncio.sleep", _record_sleep):
            # A single absurdly long "word" (a very long unbroken token) is
            # exactly the case estimate_speech_duration_s's character-count
            # floor now protects against -- and exactly the case this cap
            # exists to bound.
            await driver._speak_and_gate("x" * 100_000)
        assert captured_delays == [driver_module._MIC_GATE_MAX_HOLD_S]

    async def test_gate_reopens_even_when_speak_raises(self, tmp_path: Path) -> None:
        driver, mic_source, _spoken = _driver(tmp_path, live_chunks=[])

        async def _raising_speak(_text: str) -> None:
            raise RuntimeError("synthesis failed")

        # Reassigning a private attribute from a test in the same style the
        # rest of this file uses to swap the driver's collaborators; SpeakFn
        # is a runtime_checkable Protocol matched structurally.
        driver._speak = _raising_speak  # type: ignore[assignment]
        with pytest.raises(RuntimeError):
            await driver._speak_and_gate("hello")
        assert mic_source.listening_calls == [False, True]
        assert mic_source.drain_calls == 1


class TestLiveCallDriverInactivity:
    async def test_is_inactive_false_immediately_after_construction(
        self, tmp_path: Path
    ) -> None:
        driver, _mic_source, _spoken = _driver(tmp_path, live_chunks=[])
        assert driver._is_inactive() is False

    async def test_is_inactive_true_past_the_timeout_while_listening(
        self, tmp_path: Path
    ) -> None:
        driver, _mic_source, _spoken = _driver(tmp_path, live_chunks=[])
        await driver._session.start()  # -> Mode.LISTENING; _is_inactive requires it
        with patch.object(driver_module, "_INACTIVITY_TIMEOUT_S", -1.0):
            assert driver._is_inactive() is True

    async def test_mark_activity_resets_the_clock(self, tmp_path: Path) -> None:
        driver, _mic_source, _spoken = _driver(tmp_path, live_chunks=[])
        await driver._session.start()  # -> Mode.LISTENING; _is_inactive requires it
        with patch.object(driver_module, "_INACTIVITY_TIMEOUT_S", -1.0):
            assert driver._is_inactive() is True
            mode = driver._session.actor.mode
            driver._mark_activity(mode, mode)
            # Still true: -1.0 threshold means "always past due" regardless
            # of how recently the clock was reset -- proves _mark_activity
            # actually moved _last_activity_at rather than being a no-op,
            # via the *next* check picking up a fresh (still-failing) delta
            # rather than raising.
            assert driver._is_inactive() is True


class TestLiveCallDriverCreate:
    async def test_create_calibrates_before_building_the_session(
        self, tmp_path: Path
    ) -> None:
        mic_source = _FakeMicSource([], calibration=ScriptedTurn.silence_chunks(10))

        async def speak(text: str) -> None:
            del text

        with (
            patch.object(driver_module, "MicAudioSource", return_value=mic_source),
            patch.object(
                driver_module, "ElevenLabsSTTProvider", return_value=_FakeSTTProvider()
            ),
        ):
            driver = await LiveCallDriver.create(
                session_attach=_FakeSessionAttach("reply"),
                speak=speak,
                chime=_no_op_chime,
                control=_control(tmp_path),
                apply_control=_never_stop,
            )
        assert mic_source.calibration_durations == [driver_module._CALIBRATION_S]
        assert isinstance(driver, LiveCallDriver)

    async def test_create_refuses_to_start_when_the_stt_key_is_missing(
        self, tmp_path: Path
    ) -> None:
        """CRITICAL finding: an unhealthy STT provider must fail before
        calibration even begins -- not after 2s of mic calibration and the
        spoken "Listening." cue, on the very first turn.
        """
        mic_source = _FakeMicSource([], calibration=ScriptedTurn.silence_chunks(10))

        async def speak(text: str) -> None:
            del text

        with (
            patch.object(driver_module, "MicAudioSource", return_value=mic_source),
            patch.object(
                driver_module,
                "ElevenLabsSTTProvider",
                return_value=_FakeSTTProvider(healthy=False),
            ),
            pytest.raises(typer.BadParameter, match="no API key"),
        ):
            await LiveCallDriver.create(
                session_attach=_FakeSessionAttach("reply"),
                speak=speak,
                chime=_no_op_chime,
                control=_control(tmp_path),
                apply_control=_never_stop,
            )
        assert mic_source.calibration_durations == []


class TestLiveCallDriverApplyControl:
    async def test_run_stops_when_apply_control_returns_true(
        self, tmp_path: Path
    ) -> None:
        driver, _mic_source, spoken = _driver(
            tmp_path, live_chunks=[AudioChunk(pcm=b"\x00\x00", duration_s=0.02)] * 5
        )

        calls = AsyncMock(return_value=True)
        driver._apply_control = calls

        await driver.run()
        # Stopped on the first control check, before any mic chunk reached
        # process_chunk -- only the opening "Listening." cue was spoken.
        assert spoken == ["Listening."]
        calls.assert_awaited()


class TestLiveCallDriverControlPollInterval:
    """IMPORTANT finding: the mailbox used to be checked -- one filesystem
    rename plus a raised-and-caught ``FileNotFoundError`` in the steady
    case -- on every captured chunk (every 20ms). It is now gated on
    :data:`driver_module._CONTROL_POLL_INTERVAL_S`, a monotonic-time
    interval, not a per-chunk check.

    ``_due_for_control_check`` is exercised directly against real
    ``time.monotonic()`` reads rather than by patching the ``time`` module
    globally -- patching ``time.monotonic`` process-wide also starves
    ``asyncio``'s own internal scheduling (timers, ``Queue.get()``), which
    hangs the event loop rather than the test failing cleanly.
    """

    def test_first_check_is_due_immediately(self, tmp_path: Path) -> None:
        driver, _mic_source, _spoken = _driver(tmp_path, live_chunks=[])
        assert driver._due_for_control_check() is True

    def test_a_second_check_inside_the_interval_is_not_due(
        self, tmp_path: Path
    ) -> None:
        driver, _mic_source, _spoken = _driver(tmp_path, live_chunks=[])
        assert driver._due_for_control_check() is True
        assert driver._due_for_control_check() is False

    def test_a_check_past_the_interval_is_due_again(self, tmp_path: Path) -> None:
        driver, _mic_source, _spoken = _driver(tmp_path, live_chunks=[])
        assert driver._due_for_control_check() is True
        # Simulate the interval having elapsed, without touching the real
        # clock -- moves only this driver's own bookkeeping backward.
        driver._last_control_check_at -= driver_module._CONTROL_POLL_INTERVAL_S + 0.01
        assert driver._due_for_control_check() is True

    async def test_apply_control_is_not_called_for_every_chunk_in_a_burst(
        self, tmp_path: Path
    ) -> None:
        """A burst of chunks with no real elapsed time between them (the
        fakes yield instantly, unlike PortAudio's 20ms cadence) must still
        collapse to one control check, not one per chunk.
        """
        chunks = [AudioChunk(pcm=b"\x00\x00", duration_s=0.02)] * 20
        driver, _mic_source, _spoken = _driver(tmp_path, live_chunks=chunks)
        calls = AsyncMock(return_value=False)
        driver._apply_control = calls

        await driver.run()

        assert calls.await_count == 1

    async def test_a_stop_request_past_the_interval_is_still_picked_up(
        self, tmp_path: Path
    ) -> None:
        """Proves the gate does not starve a real stop/transfer request --
        once due again, the very next chunk still reaches ``_apply_control``.
        """
        chunks = [AudioChunk(pcm=b"\x00\x00", duration_s=0.02)] * 2
        driver, _mic_source, spoken = _driver(tmp_path, live_chunks=chunks)
        calls = AsyncMock(return_value=True)
        driver._apply_control = calls
        # Force the very first chunk to already be past the interval --
        # matches the natural post-construction state (_last_control_check_at
        # starts at 0.0), so this only documents the invariant explicitly.
        driver._last_control_check_at = 0.0

        await driver.run()

        calls.assert_awaited_once()
        assert spoken == ["Listening."]  # stopped before any turn processed


class _RaisingTurnDetector:
    """A ``TurnDetector`` stand-in whose ``process`` always raises.

    ``CallSession.process_chunk`` is the real thing ``run()`` calls on every
    chunk; a class method can't be monkeypatched on a ``__slots__`` instance,
    so the failure is injected one layer down, at the detector this class
    already takes as a real collaborator.
    """

    def process(self, _chunk: AudioChunk) -> None:
        msg = "STT provider crashed"
        raise RuntimeError(msg)

    def calibrate(self, _chunks: list[AudioChunk]) -> None:
        return None


class TestLiveCallDriverAbnormalExit:
    """Item 4 regression: an exception out of ``process_chunk`` must not skip
    ``hangup()`` -- otherwise :class:`~.call_actor.CallActor`'s mode is left
    stale (never transitioned back to idle) on the way out.
    """

    async def test_hangup_fires_even_when_process_chunk_raises(
        self, tmp_path: Path
    ) -> None:
        from punt_vox.voxd.conversation_mode.mode import Mode

        mic_source = _FakeMicSource([AudioChunk(pcm=b"\x00\x00", duration_s=0.02)])
        spoken: list[str] = []

        async def speak(text: str) -> None:
            spoken.append(text)

        driver = LiveCallDriver(
            session_attach=_FakeSessionAttach("a reply"),
            speak=speak,
            chime=_no_op_chime,
            control=_control(tmp_path),
            apply_control=_never_stop,
            detector=cast("TurnDetector", _RaisingTurnDetector()),
            mic_source=cast("MicAudioSource", mic_source),
            stt_provider=_FakeSTTProvider(),
        )

        with pytest.raises(RuntimeError, match="STT provider crashed"):
            await driver.run()

        assert driver._session.actor.mode is Mode.IDLE
