"""Tests for punt_vox.commands.call_scripted."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
import typer

from punt_vox.commands.call_scripted import (
    ScriptedCallDriver,
    ScriptedSTTProvider,
    ScriptedTurn,
)
from punt_vox.voxd.conversation_mode.audio_chunk import AudioChunk
from punt_vox.voxd.conversation_mode.call_control import CallControl
from punt_vox.voxd.conversation_mode.call_session import CallSession
from punt_vox.voxd.conversation_mode.mode import Mode
from punt_vox.voxd.conversation_mode.reply import ReplyChunk
from punt_vox.voxd.conversation_mode.session_attach import BareAuthMissingError
from punt_vox.voxd.conversation_mode.turn_detector import TurnDetector


def _script_file(tmp_path: Path, lines: list[dict[str, object]]) -> Path:
    path = tmp_path / "script.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines))
    return path


def test_scripted_turn_reads_a_jsonl_script(tmp_path: Path) -> None:
    path = _script_file(
        tmp_path,
        [
            {"text": "hello", "confidence": 0.9},
            {"text": "goodbye", "confidence": 0.95},
        ],
    )
    turns = ScriptedTurn.read_script(path)
    assert [t.text for t in turns] == ["hello", "goodbye"]
    assert [t.confidence for t in turns] == [0.9, 0.95]


def test_scripted_turn_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "script.jsonl"
    path.write_text('{"text": "a", "confidence": 1.0}\n\n\n')
    turns = ScriptedTurn.read_script(path)
    assert len(turns) == 1


def test_read_script_raises_bad_parameter_for_a_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.jsonl"
    with pytest.raises(typer.BadParameter, match="cannot read script file"):
        ScriptedTurn.read_script(missing)


def test_read_script_raises_bad_parameter_for_malformed_json_naming_the_line(
    tmp_path: Path,
) -> None:
    path = tmp_path / "script.jsonl"
    path.write_text('{"text": "ok", "confidence": 0.9}\nnot json at all\n')
    with pytest.raises(typer.BadParameter, match=r"line 2"):
        ScriptedTurn.read_script(path)


def test_read_script_raises_bad_parameter_for_a_missing_key(tmp_path: Path) -> None:
    path = tmp_path / "script.jsonl"
    path.write_text('{"text": "missing confidence key"}\n')
    with pytest.raises(typer.BadParameter, match=r"line 1"):
        ScriptedTurn.read_script(path)


def test_synthetic_chunks_are_speech_then_silence() -> None:
    turn = ScriptedTurn(text="hi", confidence=0.9)
    chunks = turn.synthetic_chunks()
    # First chunks are non-silent (speech); trailing chunks are silent.
    assert any(chunk.pcm != chunks[-1].pcm for chunk in chunks[:5])
    assert chunks[-1].pcm == chunks[-2].pcm  # the silence run is uniform


def test_silence_chunks_are_all_silent() -> None:
    chunks = ScriptedTurn.silence_chunks(5)
    assert len(chunks) == 5
    assert all(c.pcm == chunks[0].pcm for c in chunks)


async def _empty_chunks() -> AsyncIterator[AudioChunk]:
    return
    yield  # pragma: no cover -- makes this an async generator with no items


class TestScriptedSTTProvider:
    def test_name(self) -> None:
        assert ScriptedSTTProvider([]).name == "scripted"

    async def test_replays_turns_in_order(self) -> None:
        turns = [
            ScriptedTurn(text="one", confidence=0.9),
            ScriptedTurn(text="two", confidence=0.95),
        ]
        provider = ScriptedSTTProvider(turns)
        (first,) = [event async for event in provider.transcribe(_empty_chunks())]
        (second,) = [event async for event in provider.transcribe(_empty_chunks())]
        assert first.text == "one"
        assert second.text == "two"

    def test_check_health_reports_no_checks(self) -> None:
        assert ScriptedSTTProvider([]).check_health() == []


def _control(tmp_path: Path) -> CallControl:
    return CallControl(tmp_path / "call.control")


async def _never_stop(
    control: CallControl, session: CallSession, speak: object
) -> bool:
    del control, session, speak
    return False


class _RecordingSpeak:
    """A ``SpeakFn`` that records every phrase it was asked to speak."""

    def __init__(self) -> None:
        self.said: list[str] = []

    async def __call__(self, text: str) -> None:
        self.said.append(text)


class _BareAuthSessionAttach:
    """A ``SessionAttach`` that always raises ``BareAuthMissingError``.

    Stands in for ``ClaudeSessionAttach`` discovering the key is gone at
    spawn time (its own last-line-of-defense check), bypassing
    :meth:`ScriptedCallDriver.create`'s pre-flight so this test proves the
    *loop's own hangup guard*, not just the pre-flight.
    """

    def __init__(self) -> None:
        # An instance attribute, not a literal, so mypy cannot narrow the
        # branch below to "always true" and flag the trailing ``yield`` as
        # unreachable -- genuinely unreachable at runtime, but the method
        # still has to be shaped as an async generator to satisfy
        # SessionAttach.send_turn's return type.
        self._always_fails = True

    async def send_turn(self, turn: object) -> AsyncIterator[ReplyChunk]:
        del turn
        if self._always_fails:
            raise BareAuthMissingError.for_missing_key()
        yield ReplyChunk(  # pragma: no cover -- unreachable
            text="", is_final=True
        )


class TestScriptedCallDriverRun:
    async def test_run_ends_cleanly_with_one_message_when_bare_auth_fails_mid_call(
        self, tmp_path: Path
    ) -> None:
        """Regression: a scripted call whose session-attach discovers a
        missing ``ANTHROPIC_API_KEY`` mid-turn must end with exactly one
        spoken message (ReplyRecovery's own goodbye) -- not crash with
        ``IllegalTransitionError`` from an unconditional ``hangup()`` after
        the loop, which would also trigger a second, contradictory message
        from call.py's outer boundary handler.
        """
        speak = _RecordingSpeak()

        async def chime() -> None:
            return None

        turns = [ScriptedTurn(text="hello", confidence=0.9)]
        session = CallSession(
            turn_detector=ScriptedTurn.calibrated_detector(),
            stt_provider=ScriptedSTTProvider(turns),
            session_attach=_BareAuthSessionAttach(),
            speak=speak,
            chime=chime,
        )
        driver = ScriptedCallDriver(
            session=session,
            turns=turns,
            control=_control(tmp_path),
            speak=speak,
            apply_control=_never_stop,
        )

        await driver.run()  # must not raise IllegalTransitionError

        assert session.actor.mode is Mode.IDLE
        goodbyes = [phrase for phrase in speak.said if "Ending the call now" in phrase]
        assert len(goodbyes) == 1


class _RaisingTurnDetector:
    """A ``TurnDetector`` stand-in whose ``process`` always raises."""

    def process(self, _chunk: AudioChunk) -> None:
        msg = "STT provider crashed"
        raise RuntimeError(msg)

    def calibrate(self, _chunks: list[AudioChunk]) -> None:
        return None


class TestScriptedCallDriverAbnormalExit:
    """An exception out of ``process_chunk`` must not skip ``hangup()`` --
    otherwise :class:`~.call_actor.CallActor`'s mode is left stale (never
    transitioned back to idle) on the way out.
    """

    async def test_hangup_fires_even_when_process_chunk_raises(
        self, tmp_path: Path
    ) -> None:
        speak = _RecordingSpeak()

        async def chime() -> None:
            return None

        turns = [ScriptedTurn(text="hello", confidence=0.9)]
        session = CallSession(
            turn_detector=cast("TurnDetector", _RaisingTurnDetector()),
            stt_provider=ScriptedSTTProvider(turns),
            session_attach=_BareAuthSessionAttach(),
            speak=speak,
            chime=chime,
        )
        driver = ScriptedCallDriver(
            session=session,
            turns=turns,
            control=_control(tmp_path),
            speak=speak,
            apply_control=_never_stop,
        )

        with pytest.raises(RuntimeError, match="STT provider crashed"):
            await driver.run()

        assert session.actor.mode is Mode.IDLE
