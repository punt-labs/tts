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


class _SpyTurnTimer:
    """Records every ``mark()`` call, for asserting on stage order/content
    without touching real logging."""

    def __init__(self) -> None:
        self.marks: list[tuple[str, str | None]] = []

    def mark(self, stage: str, *, detail: str | None = None) -> None:
        self.marks.append((stage, detail))


class TestTurnTimerMarks:
    """``vox call``'s turn-latency trace: CallSession must call ``mark()`` at
    each documented stage, in order, exactly once per turn.
    """

    async def test_full_round_trip_marks_every_stage_in_order(self) -> None:
        timer = _SpyTurnTimer()
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
            turn_timer=timer,
        )
        await session.start()
        await _feed_one_turn(session)

        stages = [stage for stage, _detail in timer.marks]
        assert stages == [
            "speech_first_detected",
            "turn_ended",
            "stt_request_sent",
            "stt_response_received",
            "claude_spawned",
            "first_reply_frame",
            "reply_complete",
            "tts_request_sent",
            "playback_started",
        ]

    async def test_speech_first_detected_marks_once_not_per_chunk(self) -> None:
        """The detector reports SPEECH_CONTINUING for every above-threshold
        chunk, not just the first -- the rising edge, not every chunk."""
        timer = _SpyTurnTimer()
        session = CallSession(
            turn_detector=_detector(),
            stt_provider=FakeSTTProvider(
                [TranscriptEvent(text="hi", confidence=0.95, is_final=True)]
            ),
            session_attach=FakeSessionAttach(
                (ScriptedChunk(ReplyChunk(text="reply", is_final=True)),)
            ),
            speak=_Recorder(),
            turn_timer=timer,
        )
        await session.start()
        await _feed_one_turn(session)

        detected = [s for s, _d in timer.marks if s == "speech_first_detected"]
        assert len(detected) == 1

    async def test_speech_first_detected_marks_once_across_a_mid_turn_dip(
        self,
    ) -> None:
        """Regression: TurnDetector tolerates a within-word amplitude dip
        shorter than its silence-gap threshold (0.2s / 10 chunks at 20ms
        each) -- a real utterance can produce SPEECH_CONTINUING -> SILENCE
        -> SPEECH_CONTINUING *within one turn*. A previous-chunk comparison
        would re-fire speech_first_detected on the dip, resetting the
        timer's turn-start clock mid-turn -- exactly the number this
        feature exists to report.
        """
        timer = _SpyTurnTimer()
        session_attach = FakeSessionAttach(
            (ScriptedChunk(ReplyChunk(text="reply", is_final=True)),)
        )
        session = CallSession(
            turn_detector=_detector(),
            stt_provider=FakeSTTProvider(
                [TranscriptEvent(text="hi there", confidence=0.95, is_final=True)]
            ),
            session_attach=session_attach,
            speak=_Recorder(),
            turn_timer=timer,
        )
        await session.start()

        for _ in range(10):
            await session.process_chunk(_speech_chunk())
        # A brief dip -- 3 silence chunks (60ms), well under the 10-chunk
        # (200ms) silence-gap threshold that would actually end the turn.
        for _ in range(3):
            await session.process_chunk(_silence_chunk())
        for _ in range(10):
            await session.process_chunk(_speech_chunk())
        # The real silence gap that closes the turn.
        for _ in range(10):
            await session.process_chunk(_silence_chunk())

        assert session_attach.turns() == ["hi there"]
        detected = [s for s, _d in timer.marks if s == "speech_first_detected"]
        assert len(detected) == 1

    async def test_stt_response_received_carries_the_confidence_detail(self) -> None:
        timer = _SpyTurnTimer()
        session = CallSession(
            turn_detector=_detector(),
            stt_provider=FakeSTTProvider(
                [TranscriptEvent(text="hi", confidence=0.87, is_final=True)]
            ),
            session_attach=FakeSessionAttach(
                (ScriptedChunk(ReplyChunk(text="reply", is_final=True)),)
            ),
            speak=_Recorder(),
            turn_timer=timer,
        )
        await session.start()
        await _feed_one_turn(session)

        details = dict(timer.marks)
        assert details["stt_response_received"] == "confidence=0.87"

    async def test_stt_response_received_reports_no_transcript_when_ambiguous(
        self,
    ) -> None:
        """FR-19's ambiguous-capture path (no events at all) still marks the
        stage, with a detail that says why there's no confidence to report."""
        timer = _SpyTurnTimer()
        session = CallSession(
            turn_detector=_detector(),
            stt_provider=FakeSTTProvider([]),  # yields nothing
            session_attach=FakeSessionAttach(),
            speak=_Recorder(),
            turn_timer=timer,
        )
        await session.start()
        await _feed_one_turn(session)

        details = dict(timer.marks)
        assert details["stt_response_received"] == "no transcript"
        # The turn never reached session-attach -- no claude_spawned mark.
        assert "claude_spawned" not in details

    async def test_no_turn_timer_passed_defaults_to_a_real_logging_timer(self) -> None:
        """Every existing call site that never passes turn_timer must keep
        working -- construction alone must not raise, and marks must not
        error even though nothing asserts on where they land here."""
        session = CallSession(
            turn_detector=_detector(),
            stt_provider=FakeSTTProvider(
                [TranscriptEvent(text="hi", confidence=0.95, is_final=True)]
            ),
            session_attach=FakeSessionAttach(
                (ScriptedChunk(ReplyChunk(text="reply", is_final=True)),)
            ),
            speak=_Recorder(),
        )
        await session.start()
        await _feed_one_turn(session)  # must not raise
