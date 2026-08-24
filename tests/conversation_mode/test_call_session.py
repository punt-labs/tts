"""End-to-end tests for :class:`CallSession`: capture -> turn -> STT -> reply -> speech.

Drives the real :class:`TurnDetector` and :class:`CallActor` against
:class:`FakeSTTProvider` and :class:`FakeSessionAttach` -- no daemon, no
subprocess, no audio hardware -- exercising the full round trip: a closed
turn produces exactly one transcribed turn sent through session-attach, and
the reply is spoken through the injected ``speak`` callable.
"""

from __future__ import annotations

import struct
from collections.abc import AsyncIterator

from conversation_mode._session_attach_fakes import FakeSessionAttach, ScriptedChunk
from conversation_mode._stt_fakes import FakeSTTProvider
from punt_vox.types import HealthCheck
from punt_vox.types_provider_errors import ProviderAuthError
from punt_vox.voxd.conversation_mode.audio_chunk import AudioChunk
from punt_vox.voxd.conversation_mode.call_session import CallSession
from punt_vox.voxd.conversation_mode.mode import Mode
from punt_vox.voxd.conversation_mode.reply import ReplyChunk
from punt_vox.voxd.conversation_mode.reply_begins import ReplyBegins
from punt_vox.voxd.conversation_mode.reply_ends import ReplyEnds
from punt_vox.voxd.conversation_mode.stt_provider import TranscriptEvent
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
        session.actor.apply(TurnDetected())
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
        session.actor.apply(TurnDetected())
        session.actor.apply(ReplyBegins())
        await _feed_one_turn(session)  # ignored: mode is speaking
        session.actor.apply(ReplyEnds())
        assert session.actor.mode is Mode.LISTENING

        await _feed_one_turn(session)

        assert session_attach.turns() == ["hello"]


class _SequencedSTTProvider:
    """Yields one fixed-confidence transcript per ``transcribe`` call, in order.

    Unlike :class:`FakeSTTProvider` (which replays the same script every
    call), this lets a test give two successive turns distinct texts -- the
    shape needed to prove the second turn's text, not the first's, is what
    gets folded into the next real turn.
    """

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)

    @property
    def name(self) -> str:
        return "sequenced-fake"

    async def transcribe(
        self, chunks: AsyncIterator[AudioChunk]
    ) -> AsyncIterator[TranscriptEvent]:
        async for _ in chunks:
            pass
        text = self._texts.pop(0)
        yield TranscriptEvent(text=text, confidence=0.95, is_final=True)

    def check_health(self) -> list[HealthCheck]:
        return [HealthCheck(passed=True, message="sequenced fake always healthy")]


class _FailThenSucceedSTTProvider:
    """Raises on its first ``transcribe`` call, yields *text* on every call after.

    Proves a session recovers from a transient STT failure and accepts the
    very next turn cleanly -- a single-script fake can't distinguish "the
    first call" from later ones the way this test needs.
    """

    def __init__(self, *, text: str) -> None:
        self._text = text
        self._calls = 0

    @property
    def name(self) -> str:
        return "fail-then-succeed-fake"

    async def transcribe(
        self, chunks: AsyncIterator[AudioChunk]
    ) -> AsyncIterator[TranscriptEvent]:
        async for _ in chunks:
            pass
        self._calls += 1
        if self._calls == 1:
            msg = "rate limited"
            raise RuntimeError(msg)
        yield TranscriptEvent(text=self._text, confidence=0.95, is_final=True)

    def check_health(self) -> list[HealthCheck]:
        return [HealthCheck(passed=True, message="fake always healthy")]


class TestSessionAttachFailureRecovery:
    """A ``SessionAttachError`` mid-turn must not end the
    call -- the human should hear an apology and keep talking, the same
    recovery shape the low-confidence STT path already gets.
    """

    async def test_session_attach_error_speaks_an_apology_and_keeps_the_call_alive(
        self,
    ) -> None:
        speak = _Recorder()
        stt = FakeSTTProvider(
            [TranscriptEvent(text="what does this do", confidence=0.95, is_final=True)]
        )
        session_attach = FakeSessionAttach(attach_error="claude exited 1")
        session = CallSession(
            turn_detector=_detector(),
            stt_provider=stt,
            session_attach=session_attach,
            speak=speak,
        )
        await session.start()

        await _feed_one_turn(session)  # must not raise

        assert session.actor.mode is Mode.LISTENING
        assert any("wrong" in phrase.lower() for phrase in speak.said)

    async def test_a_turn_after_a_session_attach_failure_is_forwarded_normally(
        self,
    ) -> None:
        """Recovery must leave the call in a state that accepts the next turn."""
        speak = _Recorder()
        stt = _SequencedSTTProvider(["first question", "second question"])
        session_attach = FakeSessionAttach(attach_error="claude exited 1")
        session = CallSession(
            turn_detector=_detector(),
            stt_provider=stt,
            session_attach=session_attach,
            speak=speak,
        )
        await session.start()
        await _feed_one_turn(session)
        assert session.actor.mode is Mode.LISTENING

        # A second, healthy session-attach receives the next turn cleanly.
        healthy_attach = FakeSessionAttach(
            (ScriptedChunk(ReplyChunk(text="reply", is_final=True)),)
        )
        session.replace_session_attach(healthy_attach)
        await _feed_one_turn(session)

        assert healthy_attach.turns() == ["second question"]
        assert session.actor.mode is Mode.LISTENING


class TestSTTTranscribeFailureRecovery:
    """A transient STT provider fault (rate limit, 5xx, network blip) during
    :meth:`~.stt_provider.STTProvider.transcribe` must not end the call --
    the human should hear the same "didn't catch that" recovery a
    low-confidence or empty result already gets.
    """

    async def test_transcribe_failure_asks_the_human_to_repeat_and_keeps_the_call_alive(
        self,
    ) -> None:
        speak = _Recorder()
        stt = FakeSTTProvider([], transcribe_error="rate limited")
        session_attach = FakeSessionAttach()
        session = CallSession(
            turn_detector=_detector(),
            stt_provider=stt,
            session_attach=session_attach,
            speak=speak,
        )
        await session.start()

        await _feed_one_turn(session)  # must not raise

        assert session.actor.mode is Mode.LISTENING
        assert session_attach.turns() == []
        assert any("repeat" in phrase.lower() for phrase in speak.said)

    async def test_a_turn_after_a_transcribe_failure_is_forwarded_normally(
        self,
    ) -> None:
        """Recovery must leave the call in a state that accepts the next turn."""
        speak = _Recorder()
        stt = _FailThenSucceedSTTProvider(text="hello")
        session_attach = FakeSessionAttach(
            (ScriptedChunk(ReplyChunk(text="reply", is_final=True)),)
        )
        session = CallSession(
            turn_detector=_detector(),
            stt_provider=stt,
            session_attach=session_attach,
            speak=speak,
        )
        await session.start()

        await _feed_one_turn(session)  # fails, asks to repeat
        assert session.actor.mode is Mode.LISTENING
        await _feed_one_turn(session)  # succeeds

        assert session_attach.turns() == ["hello"]


class _AuthFailingSTTProvider:
    """An ``STTProvider`` whose ``transcribe`` always raises ``ProviderAuthError``."""

    def __init__(self) -> None:
        # An instance attribute, not a literal, so mypy cannot narrow the
        # branch below to "always true" and flag the trailing ``yield`` as
        # unreachable.
        self._always_fails = True

    @property
    def name(self) -> str:
        return "auth-failing-fake"

    async def transcribe(
        self, chunks: AsyncIterator[AudioChunk]
    ) -> AsyncIterator[TranscriptEvent]:
        async for _ in chunks:
            pass
        if self._always_fails:
            raise ProviderAuthError("elevenlabs", 401)
        yield TranscriptEvent(  # pragma: no cover -- unreachable
            text="", confidence=0.0, is_final=True
        )

    def check_health(self) -> list[HealthCheck]:
        return []


class TestSTTProviderAuthFailure:
    """A revoked/expired STT credential is certain and permanent, unlike a
    transient fault -- the call must end with an actionable message instead
    of repeating "didn't catch that" forever against a key that will never
    work again.
    """

    async def test_ends_the_call_with_exactly_one_actionable_message(self) -> None:
        speak = _Recorder()
        session_attach = FakeSessionAttach()
        session = CallSession(
            turn_detector=_detector(),
            stt_provider=_AuthFailingSTTProvider(),
            session_attach=session_attach,
            speak=speak,
        )
        await session.start()

        await _feed_one_turn(session)  # must not raise

        assert session.actor.mode is Mode.IDLE
        assert session_attach.turns() == []
        rejections = [phrase for phrase in speak.said if "credentials" in phrase]
        assert len(rejections) == 1
        assert "repeat" not in rejections[0].lower()


class TestCaptureDuringWait:
    """A second closed turn detected while the call is already
    ``waiting`` on the first turn's reply must be held as a pending addendum
    (docs/conversation-mode-call-state.tex section 5), never forwarded as a
    concurrent turn (which would violate CallState's ``turn_detected``
    precondition and crash the call) and never silently dropped.
    """

    async def test_a_second_turn_detected_while_waiting_is_captured_not_crashed(
        self,
    ) -> None:
        speak = _Recorder()
        stt = _SequencedSTTProvider(["first question", "second question"])
        session_attach = FakeSessionAttach()
        session = CallSession(
            turn_detector=_detector(),
            stt_provider=stt,
            session_attach=session_attach,
            speak=speak,
        )
        await session.start()
        # Force the call into waiting without going through the real
        # (blocking-on-reply) round trip -- CallState's turn_detected
        # requires mode=listening, so this alone puts the machine exactly
        # where a genuinely slow claude reply would leave it.
        session.actor.apply(TurnDetected())
        assert session.actor.mode is Mode.WAITING

        await _feed_one_turn(session)  # the second turn, closing mid-wait

        # CaptureDuringWait: waiting -> waiting.
        assert session.actor.mode is Mode.WAITING
        assert session.actor.has_pending_addendum is True
        assert session_attach.turns() == []  # never forwarded as a second turn

    async def test_the_pending_addendum_is_folded_into_the_next_real_turn(self) -> None:
        speak = _Recorder()
        stt = _SequencedSTTProvider(["addendum text", "the real next turn"])
        session_attach = FakeSessionAttach(
            (ScriptedChunk(ReplyChunk(text="reply", is_final=True)),)
        )
        session = CallSession(
            turn_detector=_detector(),
            stt_provider=stt,
            session_attach=session_attach,
            speak=speak,
        )
        await session.start()
        session.actor.apply(TurnDetected())
        await _feed_one_turn(session)  # captured as the pending addendum
        assert session.actor.has_pending_addendum is True

        # Return to listening the same way a completed reply does --
        # discharges CallState's flag, per its own invariant, but this
        # class's own stored addendum text survives that discharge.
        session.actor.apply(ReplyBegins())
        session.actor.apply(ReplyEnds())
        actor = session.actor
        assert actor.mode is Mode.LISTENING
        assert actor.has_pending_addendum is False

        await _feed_one_turn(session)  # the next real turn

        assert session_attach.turns() == ["addendum text the real next turn"]

    async def test_a_second_capture_during_wait_folds_onto_the_first_not_overwrites(
        self,
    ) -> None:
        """FR-9: the human speaking twice while still waiting must not lose
        the first utterance -- both must survive into the eventual turn."""
        speak = _Recorder()
        stt = _SequencedSTTProvider(
            ["first addendum", "second addendum", "the real next turn"]
        )
        session_attach = FakeSessionAttach(
            (ScriptedChunk(ReplyChunk(text="reply", is_final=True)),)
        )
        session = CallSession(
            turn_detector=_detector(),
            stt_provider=stt,
            session_attach=session_attach,
            speak=speak,
        )
        await session.start()
        session.actor.apply(TurnDetected())
        await _feed_one_turn(session)  # first CaptureDuringWait
        await _feed_one_turn(session)  # second CaptureDuringWait
        assert session.actor.mode is Mode.WAITING
        assert session.actor.has_pending_addendum is True

        session.actor.apply(ReplyBegins())
        session.actor.apply(ReplyEnds())
        await _feed_one_turn(session)  # the next real turn

        assert session_attach.turns() == [
            "first addendum second addendum the real next turn"
        ]


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
            "ack_spoken",
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

    async def test_capture_during_wait_then_reply_begins_clears_pending_capture(
        self,
    ) -> None:
        """Regression: a run the human never finished before the reply came
        back (no ``TURN_ENDED`` to close it) must not survive into the next
        turn -- stale ``PendingCapture`` state would fold old PCM into the
        next transcript and suppress the next turn's own
        ``speech_first_detected`` mark, anchoring :class:`TurnTimer` to the
        wrong turn's speech onset.
        """
        timer = _SpyTurnTimer()
        session_attach = FakeSessionAttach(
            (ScriptedChunk(ReplyChunk(text="reply", is_final=True)),)
        )
        session = CallSession(
            turn_detector=_detector(),
            stt_provider=FakeSTTProvider(
                [TranscriptEvent(text="next turn", confidence=0.95, is_final=True)]
            ),
            session_attach=session_attach,
            speak=_Recorder(),
            turn_timer=timer,
        )
        await session.start()
        session.actor.apply(TurnDetected())  # -> WAITING, as if a reply is in flight

        # The human starts talking again, but the reply arrives before this
        # run closes -- no TURN_ENDED for it, so process_chunk's own
        # TURN_ENDED-triggered clearing never fires.
        for _ in range(5):
            await session.process_chunk(_speech_chunk())
        in_progress_before_reply: bool = session._capture.in_progress
        assert in_progress_before_reply is True

        session.actor.apply(ReplyBegins())  # the reply comes back mid-utterance

        in_progress_after_reply: bool = session._capture.in_progress
        assert in_progress_after_reply is False

        session.actor.apply(ReplyEnds())
        timer.marks.clear()
        await _feed_one_turn(session)  # the next real turn

        assert session_attach.turns() == ["next turn"]
        detected = [s for s, _d in timer.marks if s == "speech_first_detected"]
        assert len(detected) == 1

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
