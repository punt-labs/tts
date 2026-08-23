"""End-to-end tests for :class:`CallSession`: capture -> turn -> STT -> reply -> speech.

Drives the real :class:`TurnDetector` and :class:`CallActor` against
:class:`FakeSTTProvider` and :class:`FakeSessionAttach` -- no daemon, no
subprocess, no audio hardware -- exercising the full round trip: a closed
turn produces exactly one transcribed turn sent through session-attach, and
the reply is spoken through the injected ``speak`` callable.
"""

from __future__ import annotations

import struct

from conversation_mode._session_attach_fakes import FakeSessionAttach, ScriptedChunk
from conversation_mode._stt_fakes import FakeSTTProvider
from punt_vox.voxd.conversation_mode.audio_chunk import AudioChunk
from punt_vox.voxd.conversation_mode.call_session import CallSession
from punt_vox.voxd.conversation_mode.mode import Mode
from punt_vox.voxd.conversation_mode.reply import ReplyChunk
from punt_vox.voxd.conversation_mode.reply_begins import ReplyBegins
from punt_vox.voxd.conversation_mode.reply_ends import ReplyEnds
from punt_vox.voxd.conversation_mode.stt_provider import TranscriptEvent
from punt_vox.voxd.conversation_mode.turn import TranscribedTurn
from punt_vox.voxd.conversation_mode.turn_detected import TurnDetected
from punt_vox.voxd.conversation_mode.turn_detector import TurnDetector

_CHUNK_S = 0.02


def _pcm(amplitude: int, sample_count: int = 320) -> bytes:
    return struct.pack(f"<{sample_count}h", *([amplitude] * sample_count))


def _speech_chunk() -> AudioChunk:
    return AudioChunk(pcm=_pcm(20000), duration_s=_CHUNK_S)


def _silence_chunk() -> AudioChunk:
    return AudioChunk(pcm=_pcm(0), duration_s=_CHUNK_S)


def _detector() -> TurnDetector:
    detector = TurnDetector(silence_gap_s=0.2, min_speech_s=0.3)
    detector.calibrate([_silence_chunk() for _ in range(10)])
    return detector


class _Recorder:
    """A ``SpeakFn`` that records every phrase it was asked to speak."""

    def __init__(self) -> None:
        self.said: list[str] = []

    async def __call__(self, text: str) -> None:
        self.said.append(text)


async def _noop_speak(text: str) -> None:
    return None


async def _feed_one_turn(session: CallSession) -> None:
    """Feed exactly one closed turn's worth of chunks (400ms speech + gap)."""
    for _ in range(20):
        await session.process_chunk(_speech_chunk())
    for _ in range(10):
        await session.process_chunk(_silence_chunk())


async def test_full_round_trip_speaks_the_agent_s_reply() -> None:
    speak = _Recorder()
    stt = FakeSTTProvider(
        [TranscriptEvent(text="what does this do", confidence=0.95, is_final=True)]
    )
    session_attach = FakeSessionAttach(
        (ScriptedChunk(ReplyChunk(text="It returns the sum.", is_final=True)),)
    )
    session = CallSession(
        turn_detector=_detector(),
        stt_provider=stt,
        session_attach=session_attach,
        speak=speak,
    )

    await session.start()
    await _feed_one_turn(session)

    assert session_attach.turns() == ["what does this do"]
    assert "It returns the sum." in speak.said
    assert session.actor.mode is Mode.LISTENING


async def test_low_confidence_transcript_asks_the_human_to_repeat() -> None:
    """FR-19: never fabricate on a low-confidence guess."""
    speak = _Recorder()
    stt = FakeSTTProvider(
        [TranscriptEvent(text="turn on the lights", confidence=0.2, is_final=True)]
    )
    session_attach = FakeSessionAttach()
    session = CallSession(
        turn_detector=_detector(),
        stt_provider=stt,
        session_attach=session_attach,
        speak=speak,
    )

    await session.start()
    await _feed_one_turn(session)

    assert session_attach.turns() == []
    assert any("repeat" in phrase.lower() for phrase in speak.said)
    assert session.actor.mode is Mode.LISTENING


async def test_stt_provider_yielding_no_events_asks_the_human_to_repeat() -> None:
    """FR-19: silence from the provider is exactly as ambiguous as a low score."""
    speak = _Recorder()
    stt = FakeSTTProvider([])  # yields nothing at all
    session_attach = FakeSessionAttach()
    session = CallSession(
        turn_detector=_detector(),
        stt_provider=stt,
        session_attach=session_attach,
        speak=speak,
    )

    await session.start()
    await _feed_one_turn(session)

    assert session_attach.turns() == []
    assert any("repeat" in phrase.lower() for phrase in speak.said)
    assert session.actor.mode is Mode.LISTENING


async def test_hangup_returns_to_idle() -> None:
    session = CallSession(
        turn_detector=_detector(),
        stt_provider=FakeSTTProvider([]),
        session_attach=FakeSessionAttach(),
        speak=_noop_speak,
    )
    await session.start()
    await session.hangup()
    assert session.actor.mode is Mode.IDLE


async def test_timeout_from_listening_returns_to_idle() -> None:
    session = CallSession(
        turn_detector=_detector(),
        stt_provider=FakeSTTProvider([]),
        session_attach=FakeSessionAttach(),
        speak=_noop_speak,
    )
    await session.start()
    await session.timeout()
    assert session.actor.mode is Mode.IDLE


async def test_start_speaks_the_listening_cue() -> None:
    """NFR-6: every call-state transition is communicated audibly."""
    speak = _Recorder()
    session = CallSession(
        turn_detector=_detector(),
        stt_provider=FakeSTTProvider([]),
        session_attach=FakeSessionAttach(),
        speak=speak,
    )
    await session.start()
    assert speak.said == ["Listening."]


async def test_replace_session_attach_redirects_subsequent_turns() -> None:
    """``/call transfer`` re-attaches a live call without ending it."""
    speak = _Recorder()
    stt = FakeSTTProvider(
        [TranscriptEvent(text="hello", confidence=0.95, is_final=True)]
    )
    old_attach = FakeSessionAttach(
        (ScriptedChunk(ReplyChunk(text="old reply", is_final=True)),)
    )
    new_attach = FakeSessionAttach(
        (ScriptedChunk(ReplyChunk(text="new reply", is_final=True)),)
    )
    session = CallSession(
        turn_detector=_detector(),
        stt_provider=stt,
        session_attach=old_attach,
        speak=speak,
    )
    await session.start()
    await _feed_one_turn(session)

    session.replace_session_attach(new_attach)
    await _feed_one_turn(session)

    assert old_attach.turns() == ["hello"]
    assert new_attach.turns() == ["hello"]
    assert "old reply" in speak.said
    assert "new reply" in speak.said


class TestProcessChunkGatedOnMode:
    """FR-8/mode.py's ``activeDetector`` axiom: only ``listening``/``waiting``
    feed the turn detector -- never ``speaking``, where the mic would be
    picking up the agent's own voice.
    """

    async def test_a_full_turn_s_worth_of_chunks_while_speaking_is_ignored(
        self,
    ) -> None:
        speak = _Recorder()
        stt = FakeSTTProvider(
            [TranscriptEvent(text="ignored", confidence=0.95, is_final=True)]
        )
        session_attach = FakeSessionAttach()
        session = CallSession(
            turn_detector=_detector(),
            stt_provider=stt,
            session_attach=session_attach,
            speak=speak,
        )
        await session.start()
        session.actor.apply(TurnDetected(turn=TranscribedTurn(text="prior turn")))
        session.actor.apply(ReplyBegins())
        assert session.actor.mode is Mode.SPEAKING

        await _feed_one_turn(session)

        assert session_attach.turns() == []

    async def test_turn_detection_resumes_correctly_after_speaking_ends(self) -> None:
        speak = _Recorder()
        stt = FakeSTTProvider(
            [TranscriptEvent(text="hello", confidence=0.95, is_final=True)]
        )
        session_attach = FakeSessionAttach()
        session = CallSession(
            turn_detector=_detector(),
            stt_provider=stt,
            session_attach=session_attach,
            speak=speak,
        )
        await session.start()
        session.actor.apply(TurnDetected(turn=TranscribedTurn(text="prior turn")))
        session.actor.apply(ReplyBegins())
        await _feed_one_turn(session)  # ignored: mode is speaking
        session.actor.apply(ReplyEnds())
        assert session.actor.mode is Mode.LISTENING

        await _feed_one_turn(session)

        assert session_attach.turns() == ["hello"]
