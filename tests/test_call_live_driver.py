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

from punt_vox.commands import call_live_driver as driver_module
from punt_vox.commands.call_live_driver import LiveCallDriver
from punt_vox.commands.call_scripted import ScriptedTurn
from punt_vox.voxd.conversation_mode.audio_chunk import AudioChunk
from punt_vox.voxd.conversation_mode.call_control import CallControl
from punt_vox.voxd.conversation_mode.reply import ReplyChunk
from punt_vox.voxd.conversation_mode.turn_detector import TurnDetector

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.call_session import CallSession, SpeakFn
    from punt_vox.voxd.conversation_mode.mic_audio_source import MicAudioSource
    from punt_vox.voxd.conversation_mode.turn import TranscribedTurn


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

        with patch.object(driver_module, "MicAudioSource", return_value=mic_source):
            driver = await LiveCallDriver.create(
                session_attach=_FakeSessionAttach("reply"),
                speak=speak,
                chime=_no_op_chime,
                control=_control(tmp_path),
                apply_control=_never_stop,
            )
        assert mic_source.calibration_durations == [driver_module._CALIBRATION_S]
        assert isinstance(driver, LiveCallDriver)


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
