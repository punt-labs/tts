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

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Protocol, Self, final, runtime_checkable

from punt_vox.voxd.conversation_mode.barge_in import BargeIn
from punt_vox.voxd.conversation_mode.call_actor import CallActor
from punt_vox.voxd.conversation_mode.end_call import EndCall
from punt_vox.voxd.conversation_mode.mode import Detector
from punt_vox.voxd.conversation_mode.reply_begins import ReplyBegins
from punt_vox.voxd.conversation_mode.reply_ends import ReplyEnds
from punt_vox.voxd.conversation_mode.start_call import StartCall
from punt_vox.voxd.conversation_mode.timeout_call import TimeoutCall
from punt_vox.voxd.conversation_mode.turn import TranscribedTurn
from punt_vox.voxd.conversation_mode.turn_detected import TurnDetected
from punt_vox.voxd.conversation_mode.turn_signal import TurnSignal

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.audio_chunk import AudioChunk
    from punt_vox.voxd.conversation_mode.session_attach import SessionAttach
    from punt_vox.voxd.conversation_mode.stt_provider import STTProvider
    from punt_vox.voxd.conversation_mode.turn_detector import TurnDetector

__all__ = ["CallSession", "SpeakFn"]

# FR-19: a transcript below this confidence is a signal to ask the human to
# repeat, never to act on. Mirrors the floor the fake-provider tests use
# (tests/conversation_mode/test_stt_provider.py), stated once here as the
# production gate.
CONFIDENCE_FLOOR = 0.6

_ASK_TO_REPEAT = "Sorry, I didn't catch that -- could you repeat it?"


@runtime_checkable
class SpeakFn(Protocol):
    """Speak *text* aloud and return once playback has started or completed.

    Async, not sync: ``VoxClientSync.synthesize`` blocks its calling thread
    for the full round trip to the daemon, and this call happens inline
    inside :class:`CallSession`'s async methods, which run on the call's
    single event loop -- a synchronous ``SpeakFn`` would stall that loop for
    the duration of every utterance, during which the microphone's capture
    queue keeps filling and a pending ``/call stop`` goes unnoticed. A
    caller backed by a blocking client wraps it in ``asyncio.to_thread``
    (see :mod:`punt_vox.commands.call`).
    """

    async def __call__(self, text: str) -> None: ...


@final
class CallSession:
    """Drives one call's audio-in, transcript, session-attach, speech-out loop."""

    __slots__ = (
        "_actor",
        "_pending_chunks",
        "_session_attach",
        "_speak",
        "_stt_provider",
        "_turn_detector",
    )
    _actor: CallActor
    _turn_detector: TurnDetector
    _stt_provider: STTProvider
    _session_attach: SessionAttach
    _speak: SpeakFn
    _pending_chunks: list[AudioChunk]

    def __new__(
        cls,
        *,
        turn_detector: TurnDetector,
        stt_provider: STTProvider,
        session_attach: SessionAttach,
        speak: SpeakFn,
    ) -> Self:
        self = super().__new__(cls)
        self._actor = CallActor()
        self._turn_detector = turn_detector
        self._stt_provider = stt_provider
        self._session_attach = session_attach
        self._speak = speak
        self._pending_chunks = []
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
        self._pending_chunks.append(chunk)
        signal = self._turn_detector.process(chunk)
        if signal is TurnSignal.TURN_ENDED:
            chunks, self._pending_chunks = self._pending_chunks, []
            await self._handle_turn_ended(chunks)

    async def _handle_turn_ended(self, chunks: list[AudioChunk]) -> None:
        final_event = None
        async for event in self._stt_provider.transcribe(_as_async_iter(chunks)):
            final_event = event

        if final_event is None or final_event.confidence < CONFIDENCE_FLOOR:
            # FR-19: never fabricate on ambiguous or failed capture -- an
            # STTProvider that raised nothing to transcribe is exactly as
            # ambiguous as one that transcribed with low confidence; both
            # ask the human to repeat rather than silently drop the turn.
            # The call stays in listening; no TurnDetected is enqueued.
            await self._speak(_ASK_TO_REPEAT)
            return

        turn = TranscribedTurn(text=final_event.text)
        self._actor.apply(TurnDetected(turn=turn))
        await self._speak_reply(turn)

    async def _speak_reply(self, turn: TranscribedTurn) -> None:
        pieces = [chunk.text async for chunk in self._session_attach.send_turn(turn)]
        self._actor.apply(ReplyBegins())
        await self._speak("".join(pieces))
        self._actor.apply(ReplyEnds())
        await self._speak("Ready.")

    async def barge_in(self) -> None:
        """FR-8: interrupt the agent's speech; speaking -> listening.

        A separate barge-in *detector* task calls this once one exists; it
        does not yet, so this transition is exercised directly in tests
        rather than through a live detector.
        """
        self._actor.apply(BargeIn())


async def _as_async_iter(chunks: list[AudioChunk]) -> AsyncIterator[AudioChunk]:
    """Return an async iterator over *chunks* for :class:`STTProvider.transcribe`.

    A tiny local helper rather than requiring every caller to build one --
    ``STTProvider.transcribe`` is typed to accept ``AsyncIterator[AudioChunk]``;
    this produces exactly that from the list :meth:`CallSession.process_chunk`
    already accumulated.
    """
    for chunk in chunks:
        yield chunk
