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
from punt_vox.types_provider_errors import ProviderAuthError
from punt_vox.voxd.conversation_mode.audio_chunk import AudioChunk
from punt_vox.voxd.conversation_mode.barge_in import BargeIn
from punt_vox.voxd.conversation_mode.call_actor import CallActor
from punt_vox.voxd.conversation_mode.capture_during_wait import CaptureDuringWait
from punt_vox.voxd.conversation_mode.end_call import EndCall
from punt_vox.voxd.conversation_mode.mode import Detector, Mode
from punt_vox.voxd.conversation_mode.pending_capture import PendingCapture
from punt_vox.voxd.conversation_mode.reply_begins import ReplyBegins
from punt_vox.voxd.conversation_mode.reply_ends import ReplyEnds
from punt_vox.voxd.conversation_mode.reply_recovery import ReplyRecovery
from punt_vox.voxd.conversation_mode.session_attach import SessionAttachError
from punt_vox.voxd.conversation_mode.speak_fn import SpeakFn
from punt_vox.voxd.conversation_mode.start_call import StartCall
from punt_vox.voxd.conversation_mode.timeout_call import TimeoutCall
from punt_vox.voxd.conversation_mode.turn import TranscribedTurn
from punt_vox.voxd.conversation_mode.turn_detected import TurnDetected
from punt_vox.voxd.conversation_mode.turn_signal import TurnSignal
from punt_vox.voxd.conversation_mode.turn_timer import LoggingTurnTimer
from punt_vox.voxd.conversation_mode.turn_transcriber import TurnTranscriber
from punt_vox.voxd.conversation_mode.wait_cue import WaitCue

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.session_attach import SessionAttach
    from punt_vox.voxd.conversation_mode.stt_provider import STTProvider
    from punt_vox.voxd.conversation_mode.turn_detector import TurnDetector
    from punt_vox.voxd.conversation_mode.turn_timer import TurnTimer
    from punt_vox.voxd.conversation_mode.wait_cue import ChimeFn

__all__ = ["CallSession", "SpeakFn"]

logger = logging.getLogger(__name__)

_ASK_TO_REPEAT = "Sorry, I didn't catch that -- could you repeat it?"

# Unlike _ASK_TO_REPEAT, a rejected STT credential is certain and permanent,
# so this ends the call instead of looping forever -- mirrors
# reply_recovery.py's own _BARE_AUTH_MISSING sentence.
_STT_AUTH_FAILED = (
    "Sorry, this call can't continue -- the speech-recognition credentials "
    "were rejected. Ending the call now."
)


@final
class CallSession:
    """Drives one call's audio-in, transcript, session-attach, speech-out loop."""

    __slots__ = (
        "_actor",
        "_capture",
        "_pending_addendum",
        "_reply_recovery",
        "_session_attach",
        "_speak",
        "_transcriber",
        "_turn_detector",
        "_turn_timer",
        "_wait_cue",
    )
    _actor: CallActor
    _turn_detector: TurnDetector
    _transcriber: TurnTranscriber
    _reply_recovery: ReplyRecovery
    _session_attach: SessionAttach
    _speak: SpeakFn
    _wait_cue: WaitCue
    """Constructed with ``chime=None`` for the scripted (``--script``) path
    and every existing test that doesn't pass one -- see that constructor
    argument's own docstring for why absence is a legitimate default, not a
    deferred decision."""
    _capture: PendingCapture
    """The run currently accumulating -- see :class:`PendingCapture` for the
    close/discard split this class relies on."""
    _pending_addendum: TranscribedTurn | None
    """FR-4/``docs/conversation-mode-call-state.tex`` section 5's pending
    addendum: speech the turn detector closed while the call was already
    ``waiting`` on the prior turn's reply. ``None`` when there is none.
    :attr:`CallActor.has_pending_addendum` tracks the same *fact* only up to
    the return to ``listening`` -- ``CallState.reply_ends``/``barge_in``
    discharge that flag as part of the transition's own invariant, but this
    attribute deliberately survives past it, since folding the addendum's
    text into the next turn (FR-9) is this class's job, not the state
    machine's, and the text has to still be here when that next turn
    arrives."""
    _turn_timer: TurnTimer

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
        self._session_attach = session_attach
        self._speak = speak
        self._reply_recovery = ReplyRecovery(self._actor, speak)
        self._wait_cue = WaitCue(chime)
        self._capture = PendingCapture()
        self._pending_addendum = None
        self._turn_timer = turn_timer if turn_timer is not None else LoggingTurnTimer()
        self._transcriber = TurnTranscriber(stt_provider, self._turn_timer)
        self._actor.on_transition(self._discard_pending_capture)
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

    def _discard_pending_capture(self, before: Mode, after: Mode) -> None:
        """Discard a :attr:`_capture` run left open past its owning mode.

        A run the human never finished (no ``TURN_ENDED``) before the mode
        moved away from listening/waiting has nothing left to close it --
        uncleared, it corrupts the next turn (see :class:`PendingCapture`'s
        own docstring). Registered as a transition observer, not inlined at
        each call site, so :class:`~.reply_recovery.ReplyRecovery`'s own
        ``apply()`` calls on this actor get the same discharge for free.
        Mirrors the transitions :class:`CallState` discharges
        ``has_pending_addendum`` on, plus ``reply_begins`` -- the
        audio-buffer staleness window opens earlier than the addendum-text
        one does.
        """
        del before
        if after in (Mode.SPEAKING, Mode.IDLE):  # ReplyBegins / EndCall / TimeoutCall
            self._capture.discard()

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
        back up -- would accumulate in :attr:`_capture` and could close as a
        fabricated "turn" the moment ``speaking`` ends.

        Otherwise: the turn detector is the sole judge of when a run of
        speech becomes a turn (FR-5/6/7); this method's only remaining job
        is to accumulate chunks for a run in progress and hand them to
        :meth:`_handle_turn_ended` once the detector says the run closed.
        """
        if self._actor.current_detector is not Detector.TURN:
            return
        # The first SPEECH_CONTINUING chunk of this run, not every
        # above-threshold chunk and not merely the one after a SILENCE
        # chunk -- see PendingCapture.note_speech's own docstring for why a
        # previous-chunk comparison is wrong here.
        signal = self._turn_detector.process(chunk)
        if signal is TurnSignal.SPEECH_CONTINUING and self._capture.note_speech():
            self._turn_timer.mark("speech_first_detected")
        self._capture.append(chunk)
        if signal is TurnSignal.TURN_ENDED:
            self._turn_timer.mark("turn_ended")
            await self._handle_turn_ended(self._capture.close())

    async def _handle_turn_ended(self, chunks: list[AudioChunk]) -> None:
        self._turn_timer.mark("stt_request_sent")
        try:
            final_event = await self._transcriber.transcribe(
                AudioChunk.as_async_iter(chunks)
            )
        except ProviderAuthError:
            # Certain and permanent, unlike a transient transcribe failure
            # -- see TurnTranscriber's own re-raise. Mode is still LISTENING
            # here, so EndCall's "any active mode" precondition already
            # holds without routing through speaking first.
            logger.exception("STT provider auth failed -- ending the call")
            await self._speak(_STT_AUTH_FAILED)
            self._actor.apply(EndCall())
            return
        if final_event is None:
            # FR-19: never fabricate on ambiguous or failed capture -- no
            # transcript, a low-confidence transcript, and a transient STT
            # provider fault are all exactly this ambiguous (see
            # :class:`TurnTranscriber`'s own docstring for why they collapse
            # to the same ``None``). The call stays in listening; no
            # TurnDetected is enqueued.
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
            if self._pending_addendum is not None:
                # A third+ utterance while still waiting: fold onto what is
                # already pending rather than overwrite it -- a second
                # CaptureDuringWait must not silently discard the first
                # addendum's text.
                turn = TranscribedTurn(
                    text=f"{self._pending_addendum.text} {turn.text}"
                )
            self._pending_addendum = turn
            return

        if self._pending_addendum is not None:
            turn = TranscribedTurn(text=f"{self._pending_addendum.text} {turn.text}")
            self._pending_addendum = None
        self._actor.apply(TurnDetected())
        await self._speak_reply(turn)

    async def _speak_reply(self, turn: TranscribedTurn) -> None:
        # The per-turn claude subprocess spawn measured 13-25s median -- an
        # instant acknowledgment covers the human's first few seconds of
        # otherwise-dead silence, before the STT->claude->TTS round trip
        # even starts. Goes through self._speak, the same mic-gated channel
        # every other cue in this flow uses, so it is never captured as if
        # the human said it.
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
        except SessionAttachError as exc:
            await self._reply_recovery.recover(exc)
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
