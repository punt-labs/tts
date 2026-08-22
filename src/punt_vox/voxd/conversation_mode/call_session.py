"""One live call: wires turn detection, STT, session-attach, and speech together.

:class:`CallSession` is the orchestration this slice exists to prove out --
capture chunks feed the turn detector; a closed run is handed to the STT
provider; a high-confidence transcript is forwarded through session-attach;
the reply is spoken. Every collaborator is a protocol or a plain callable,
so ``tests/conversation_mode/test_call_session.py`` drives the whole
pipeline against
:class:`~conversation_mode._session_attach_fakes.FakeSessionAttach` and
:class:`~conversation_mode._stt_fakes.FakeSTTProvider` with no daemon, no
subprocess, and no audio hardware.

The real ElevenLabs :class:`~.stt_provider.STTProvider` implementation and
the ``mic:call`` MCP tool wiring are deferred to a follow-up mission: both
``src/punt_vox/server.py`` and ``src/punt_vox/providers/`` are write-set-locked
by another open mission at the time this slice was built. This slice ships
against a scripted STT provider; :mod:`punt_vox.commands.call` documents the
CLI-side substitute for live microphone capture, deferred alongside it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Protocol, Self, final, runtime_checkable

from punt_vox.voxd.conversation_mode.barge_in import BargeIn
from punt_vox.voxd.conversation_mode.call_actor import CallActor
from punt_vox.voxd.conversation_mode.end_call import EndCall
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

    Matches ``VoxClientSync.synthesize``'s call shape closely enough that a
    ``lambda text: client.synthesize(text)`` satisfies it directly; kept as
    a narrow protocol rather than importing ``VoxClientSync`` here so this
    module stays free of the daemon-client dependency for testing.
    """

    def __call__(self, text: str) -> None: ...


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
        self._speak("Listening.")

    async def hangup(self) -> None:
        """FR-2's explicit end: any active mode -> idle."""
        self._actor.apply(EndCall())

    async def timeout(self) -> None:
        """FR-2's bounded-inactivity end: listening -> idle."""
        self._actor.apply(TimeoutCall())

    async def process_chunk(self, chunk: AudioChunk) -> None:
        """Feed one captured chunk to the turn detector; act on a closed run.

        The turn detector is the sole judge of when a run of speech becomes
        a turn (FR-5/6/7); this method's only job is to accumulate chunks
        for a run in progress and hand them to :meth:`_handle_turn_ended`
        once the detector says the run closed.
        """
        self._pending_chunks.append(chunk)
        signal = self._turn_detector.process(chunk)
        if signal is TurnSignal.TURN_ENDED:
            chunks, self._pending_chunks = self._pending_chunks, []
            await self._handle_turn_ended(chunks)

    async def _handle_turn_ended(self, chunks: list[AudioChunk]) -> None:
        final_event = None
        async for event in self._stt_provider.transcribe(_as_async_iter(chunks)):
            final_event = event
        if final_event is None:
            return

        if final_event.confidence < CONFIDENCE_FLOOR:
            # FR-19: never fabricate on a low-confidence guess -- ask instead.
            # The call stays in listening; no TurnDetected is enqueued.
            self._speak(_ASK_TO_REPEAT)
            return

        turn = TranscribedTurn(text=final_event.text)
        self._actor.apply(TurnDetected(turn=turn))
        await self._speak_reply(turn)

    async def _speak_reply(self, turn: TranscribedTurn) -> None:
        pieces = [chunk.text async for chunk in self._session_attach.send_turn(turn)]
        self._actor.apply(ReplyBegins())
        self._speak("".join(pieces))
        self._actor.apply(ReplyEnds())
        self._speak("Ready.")

    async def barge_in(self) -> None:
        """FR-8: interrupt the agent's speech; speaking -> listening.

        Barge-in *detection* is out of scope for this slice; this method
        exists so the transition is exercisable once a detector drives it.
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
