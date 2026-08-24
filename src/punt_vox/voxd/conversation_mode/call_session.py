"""One live call: wires turn detection, STT, session-attach, and speech together.

:class:`CallSession` is the orchestration this class exists to prove out --
capture chunks feed the turn detector; a closed run is handed to the STT
provider; a high-confidence transcript is forwarded through session-attach;
the reply is spoken. Every collaborator is a protocol or a plain callable, so
``tests/conversation_mode/test_call_session.py`` drives the whole pipeline
against
:class:`~conversation_mode._session_attach_fakes.FakeSessionAttach` and
:class:`~conversation_mode._stt_fakes.FakeSTTProvider` with no daemon, no
subprocess, and no audio hardware. Production wires the real ElevenLabs
:class:`~punt_vox.providers.elevenlabs_stt.ElevenLabsSTTProvider` and real
microphone capture (:class:`~.mic_audio_source.MicAudioSource`), both driven
from :mod:`punt_vox.commands.call`; ``vox call start --script`` swaps in the
scripted fakes' production counterparts for demos, tests, and CI.
"""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING, Self, final

from punt_vox.quips import CALL_ACK_PHRASES
from punt_vox.voxd.conversation_mode.audio_chunk import AudioChunk
from punt_vox.voxd.conversation_mode.barge_in import BargeIn
from punt_vox.voxd.conversation_mode.call_actor import CallActor
from punt_vox.voxd.conversation_mode.capture_during_wait import CaptureDuringWait
from punt_vox.voxd.conversation_mode.end_call import EndCall
from punt_vox.voxd.conversation_mode.mode import Detector, Mode
from punt_vox.voxd.conversation_mode.reply_begins import ReplyBegins
from punt_vox.voxd.conversation_mode.reply_ends import ReplyEnds
from punt_vox.voxd.conversation_mode.session_attach import SessionAttachError
from punt_vox.voxd.conversation_mode.speak_fn import SpeakFn
from punt_vox.voxd.conversation_mode.start_call import StartCall
from punt_vox.voxd.conversation_mode.timeout_call import TimeoutCall
from punt_vox.voxd.conversation_mode.turn import TranscribedTurn
from punt_vox.voxd.conversation_mode.turn_detected import TurnDetected
from punt_vox.voxd.conversation_mode.turn_signal import TurnSignal
from punt_vox.voxd.conversation_mode.turn_timer import LoggingTurnTimer
from punt_vox.voxd.conversation_mode.wait_cue import WaitCue

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.session_attach import SessionAttach
    from punt_vox.voxd.conversation_mode.stt_provider import STTProvider
    from punt_vox.voxd.conversation_mode.turn_detector import TurnDetector
    from punt_vox.voxd.conversation_mode.turn_timer import TurnTimer
    from punt_vox.voxd.conversation_mode.wait_cue import ChimeFn

__all__ = ["CallSession", "SpeakFn"]

logger = logging.getLogger(__name__)

# FR-19: a transcript below this confidence is a signal to ask the human to
# repeat, never to act on. Mirrors the floor the fake-provider tests use
# (tests/conversation_mode/test_stt_provider.py), stated once here as the
# production gate.
CONFIDENCE_FLOOR = 0.6

_ASK_TO_REPEAT = "Sorry, I didn't catch that -- could you repeat it?"

# FR-19's low-confidence path never leaves listening for the turn it can't
# act on; a SessionAttachError happens after TurnDetected already moved the
# call to waiting (see CallState.turn_detected's precondition), so recovery
# has to speak its way back through speaking -> listening rather than
# staying put -- the apology IS the reply this turn gets.
_SESSION_ATTACH_FAILED = "Sorry, something went wrong -- go ahead and try again."


@final
class CallSession:
    """Drives one call's audio-in, transcript, session-attach, speech-out loop."""

    __slots__ = (
        "_actor",
        "_pending_addendum",
        "_pending_chunks",
        "_session_attach",
        "_speak",
        "_stt_provider",
        "_turn_detector",
        "_turn_in_progress",
        "_turn_timer",
        "_wait_cue",
    )
    _actor: CallActor
    _turn_detector: TurnDetector
    _stt_provider: STTProvider
    _session_attach: SessionAttach
    _speak: SpeakFn
    _wait_cue: WaitCue
    """Constructed with ``chime=None`` for the scripted (``--script``) path
    and every existing test that doesn't pass one -- see that constructor
    argument's own docstring for why absence is a legitimate default, not a
    deferred decision."""
    _pending_chunks: list[AudioChunk]
    _pending_addendum: TranscribedTurn | None
    """FR-4/``docs/conversation-mode-call-state.tex`` section 5's pending
    addendum: speech the turn detector closed while the call was already
    ``waiting`` on the prior turn's reply. ``None`` when there is none --
    :attr:`CallActor.has_pending_addendum` mirrors this as a bool for
    ``CallState``'s own invariant, but only this class holds the actual
    transcribed text, since the Z model itself treats what happens to the
    content as out of its scope (folding it into the next turn, per FR-9,
    is this class's job, not the state machine's)."""
    _turn_timer: TurnTimer
    _turn_in_progress: bool
    """Whether ``speech_first_detected`` has already fired for the run
    currently accumulating in :attr:`_pending_chunks`. Turn-scoped, not
    previous-chunk-scoped: :class:`TurnDetector` tolerates brief
    within-word amplitude dips shorter than its silence-gap threshold (see
    that class's own docstring), so one real turn can produce
    ``SPEECH_CONTINUING -> SILENCE -> SPEECH_CONTINUING`` transitions
    *within itself*. Comparing only to the immediately-previous chunk's
    signal would re-fire the mark on every such dip, resetting
    :class:`~.turn_timer.TurnTimer`'s turn-start clock mid-turn -- exactly
    the number this whole feature exists to report. Set on the first
    ``SPEECH_CONTINUING`` chunk of a run, cleared when the run closes
    (``TURN_ENDED``), regardless of whether that turn is ultimately acted
    on or asks the human to repeat (FR-19)."""

    def __new__(
        cls,
        *,
        turn_detector: TurnDetector,
        stt_provider: STTProvider,
        session_attach: SessionAttach,
        speak: SpeakFn,
        # Absence means the caller doesn't need a substitute for tests --
        # every real call site gets a real LoggingTurnTimer(), which is
        # always safe and cheap to construct (see that class: it always
        # calls logger.debug(); whether that record goes anywhere is a
        # logging-config decision made elsewhere, not this constructor's).
        turn_timer: TurnTimer | None = None,
        # See _wait_cue's own docstring for why None is a real default, not
        # a deferred decision.
        chime: ChimeFn | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._actor = CallActor()
        self._turn_detector = turn_detector
        self._stt_provider = stt_provider
        self._session_attach = session_attach
        self._speak = speak
        self._wait_cue = WaitCue(chime)
        self._pending_chunks = []
        self._pending_addendum = None
        self._turn_timer = turn_timer if turn_timer is not None else LoggingTurnTimer()
        self._turn_in_progress = False
        return self

    @property
    def actor(self) -> CallActor:
        """Return the underlying :class:`CallActor` (mode, dispatch queue)."""
        return self._actor

    async def start(self) -> None:
        """Begin the call: idle -> listening, with the ready cue (NFR-6)."""
        self._actor.apply(StartCall())
        await self._speak("Listening.")

    async def hangup(self) -> None:
        """FR-2's explicit end: any active mode -> idle."""
        self._actor.apply(EndCall())

    async def timeout(self) -> None:
        """FR-2's bounded-inactivity end: listening -> idle."""
        self._actor.apply(TimeoutCall())

    def replace_session_attach(self, session_attach: SessionAttach) -> None:
        """Re-attach this call to a different session (``/call transfer``).

        Takes effect on the next turn's :meth:`~.session_attach.SessionAttach.send_turn`
        call -- a turn already mid-flight when a transfer request lands
        keeps talking to the session it started with, since it already has
        a reply streaming back from that session's own subprocess.
        """
        self._session_attach = session_attach

    async def process_chunk(self, chunk: AudioChunk) -> None:
        """Feed one captured chunk to the turn detector; act on a closed run.

        Ignored outright unless :attr:`~.mode.Mode.active_detector` says the
        turn detector is the one live for the call's current mode --
        :attr:`~.mode.Detector.TURN`, active in ``listening`` and
        ``waiting`` but not ``speaking`` (mirrors :mod:`.mode`'s
        ``activeDetector`` axiom, the same mapping :attr:`CallActor.mode`
        already carries). Without this gate, audio captured while the
        agent's own reply plays -- the microphone picking its own speech
        back up -- would accumulate in :attr:`_pending_chunks` and the
        detector's own run-in-progress state, and could close as a
        fabricated "turn" the moment ``speaking`` ends. Never accumulating
        it in the first place means there is nothing to discard on that
        transition, unlike the mic's own capture queue (drained separately
        by the caller via :meth:`~.mic_audio_source.MicAudioSource.drain_pending`,
        which this class has no reach into).

        Otherwise: the turn detector is the sole judge of when a run of
        speech becomes a turn (FR-5/6/7); this method's only remaining job
        is to accumulate chunks for a run in progress and hand them to
        :meth:`_handle_turn_ended` once the detector says the run closed.
        """
        if self._actor.current_detector is not Detector.TURN:
            return
        # The first SPEECH_CONTINUING chunk of this run, not every
        # above-threshold chunk and not merely the one after a SILENCE
        # chunk -- see _turn_in_progress's docstring for why a
        # previous-chunk comparison is wrong here.
        signal = self._turn_detector.process(chunk)
        if signal is TurnSignal.SPEECH_CONTINUING and not self._turn_in_progress:
            self._turn_timer.mark("speech_first_detected")
            self._turn_in_progress = True
        self._pending_chunks.append(chunk)
        if signal is TurnSignal.TURN_ENDED:
            self._turn_timer.mark("turn_ended")
            self._turn_in_progress = False
            chunks, self._pending_chunks = self._pending_chunks, []
            await self._handle_turn_ended(chunks)

    async def _handle_turn_ended(self, chunks: list[AudioChunk]) -> None:
        self._turn_timer.mark("stt_request_sent")
        final_event = None
        stt_events = self._stt_provider.transcribe(AudioChunk.as_async_iter(chunks))
        async for event in stt_events:
            final_event = event
        confidence_detail = (
            "no transcript"
            if final_event is None
            else f"confidence={final_event.confidence:.2f}"
        )
        self._turn_timer.mark("stt_response_received", detail=confidence_detail)

        if final_event is None or final_event.confidence < CONFIDENCE_FLOOR:
            # FR-19: never fabricate on ambiguous or failed capture -- an
            # STTProvider that raised nothing to transcribe is exactly as
            # ambiguous as one that transcribed with low confidence; both
            # ask the human to repeat rather than silently drop the turn.
            # The call stays in listening; no TurnDetected is enqueued.
            await self._speak(_ASK_TO_REPEAT)
            return

        turn = TranscribedTurn(text=final_event.text)
        if self._actor.mode is Mode.WAITING:
            # docs/conversation-mode-call-state.tex section 5's
            # CaptureDuringWait: the turn detector (still active while
            # waiting, per :meth:`process_chunk`'s own docstring) fired
            # again before the prior turn's reply came back. FR-4 rules out
            # dispatching a second, concurrent turn to the same session, so
            # this is held as a pending addendum rather than forwarded --
            # applying TurnDetected here instead would violate CallState's
            # own precondition (it requires mode=listening) and crash the
            # call. Folded into the next real turn's text once the call
            # returns to listening, per FR-9's same principle for barge-in
            # speech (the Z model pins only the discharge moment, not what
            # the implementation does with the content).
            self._actor.apply(CaptureDuringWait())
            self._pending_addendum = turn
            return

        if self._pending_addendum is not None:
            turn = TranscribedTurn(text=f"{self._pending_addendum.text} {turn.text}")
            self._pending_addendum = None
        self._actor.apply(TurnDetected())
        await self._speak_reply(turn)

    async def _speak_reply(self, turn: TranscribedTurn) -> None:
        # vox-36xc: the per-turn claude subprocess spawn measured 13-25s
        # median -- an instant acknowledgment covers the human's first few
        # seconds of otherwise-dead silence, before the STT->claude->TTS
        # round trip even starts. Goes through self._speak, the same
        # mic-gated channel every other cue in this flow uses, so it is
        # never captured as if the human said it.
        await self._speak(random.choice(CALL_ACK_PHRASES))
        self._turn_timer.mark("ack_spoken")

        # "claude_spawned"/"first_reply_frame" approximate a subprocess this
        # class has no visibility into: SessionAttach is a Protocol, and the
        # production ClaudeSessionAttach spawns claude -p --resume as the
        # very first thing send_turn does, before its first yield -- so
        # marking just before iterating starts, and again on the first
        # chunk received, is accurate for that implementation even though
        # this class cannot see inside it.
        self._turn_timer.mark("claude_spawned")
        pieces: list[str] = []
        first_frame_seen = False
        try:
            async with self._wait_cue.active():
                async for chunk in self._session_attach.send_turn(turn):
                    if not first_frame_seen:
                        self._turn_timer.mark("first_reply_frame")
                        first_frame_seen = True
                    pieces.append(chunk.text)
        except SessionAttachError:
            # A subprocess failure here (nonzero exit, malformed
            # stream-json, the 120s reply timeout) is exactly as
            # unactionable as the low-confidence STT path above -- the
            # human still has a live call and can just try again. Recover
            # to listening rather than letting the exception end the call:
            # waiting has no direct path back to listening (CallState has
            # no such transition), so the apology travels through
            # speaking, same as a real reply would.
            logger.exception("session-attach failed mid-turn")
            self._actor.apply(ReplyBegins())
            await self._speak(_SESSION_ATTACH_FAILED)
            self._actor.apply(ReplyEnds())
            return
        self._turn_timer.mark("reply_complete")
        self._actor.apply(ReplyBegins())
        self._turn_timer.mark("tts_request_sent")
        await self._speak("".join(pieces))
        # "playback_started" is the best available proxy, not a real signal:
        # SpeakFn's own contract (see that Protocol's docstring) is "returns
        # once playback has started OR completed" -- this class cannot tell
        # which, and a caller wrapping speak() in its own timing-sensitive
        # logic (the live path's mic-echo gate) can push this mark later
        # still, past when audio actually started.
        self._turn_timer.mark(
            "playback_started", detail="approximate -- see SpeakFn's contract"
        )
        self._actor.apply(ReplyEnds())
        await self._speak("Ready.")

    async def barge_in(self) -> None:
        """FR-8: interrupt the agent's speech; speaking -> listening.

        A separate barge-in *detector* task calls this once one exists; it
        does not yet, so this transition is exercised directly in tests
        rather than through a live detector.
        """
        self._actor.apply(BargeIn())
