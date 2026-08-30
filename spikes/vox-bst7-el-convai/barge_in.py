"""Scripted barge-in conversation: synthesized voice in, event trace out.

The audio-source seam: where run_live binds the session to a real ALSA
microphone, :class:`SyntheticAudio` streams pre-synthesized PCM plus
continuous silence at real-time pace, so the same ``ConvAISession``
exercises server-side VAD with no human at the mic. It doubles as the
session's ``AudioSink``: agent audio is counted (not played), and
``flush`` -- which the session calls on an ``interruption`` event -- is
the live barge-in signal the flow waits on.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, Self, final

from websockets.exceptions import ConnectionClosed

from convai import ConvAISession, EventTrace
from speech import EspeakSynth


class ChunkTransport(Protocol):
    """Where the synthetic mic streams its PCM; the live session satisfies it."""

    async def send_audio_chunk(self, pcm: bytes) -> None:
        """Stream one base64-encoded PCM chunk upstream."""
        ...


_RATE = 16_000
_CHUNK_BYTES = 2_048  # bytes of S16LE mono @16k -- 64ms per send
_CHUNK_SECONDS = _CHUNK_BYTES / (_RATE * 2)
_SILENCE = bytes(_CHUNK_BYTES)

# The leading words are sacrificial: EL's ASR clips the onset of the
# first utterance in a session (observed twice: "Please search the code
# for..." arrived as "The code for..." and "Hold for..."), so the verb
# that matters must not be first.
TRIGGER_TEXT = (
    "Hey there, hello, please search the code for the playback queue, thank you"
)
# Deliberately topic-neutral: an interruption that announces a change of
# subject invites the LLM to abandon the search, which confounds the
# recall probe -- the test must measure EL's state handling, not the
# agent's obedience to "ask something different".
INTERRUPT_TEXT = (
    "Wait, wait, stop, hold on one moment, wait, hold on, stop for a second"
)
PROBE_TEXT = "What did you just find?"
NOTE_TEXT = "Please write a note that says barge in state check."


@dataclass(frozen=True, slots=True)
class BargeInUtterances:
    """The four scripted utterances, pre-rendered to session-rate PCM."""

    trigger: bytes
    interrupt: bytes
    probe: bytes
    note: bytes

    @classmethod
    def synthesized(cls, synth: EspeakSynth) -> Self:
        return cls(
            trigger=synth.pcm(TRIGGER_TEXT),
            interrupt=synth.pcm(INTERRUPT_TEXT),
            probe=synth.pcm(PROBE_TEXT),
            note=synth.pcm(NOTE_TEXT),
        )


@final
class SyntheticAudio:
    """Scripted microphone plus counting sink (the audio-source seam).

    Streams silence continuously like a live mic, plays queued PCM
    utterances at real-time pace, and observes barge-in: the session
    calls ``flush`` when the server reports an interruption.
    """

    _trace: EventTrace
    _pending: deque[bytes]
    _pump: asyncio.Task[None] | None  # None until start()
    _interruption_count: int
    _interrupted: asyncio.Event
    _agent_audio_bytes: int

    def __new__(cls, trace: EventTrace) -> Self:
        self = super().__new__(cls)
        self._trace = trace
        self._pending = deque()
        self._pump = None
        self._interruption_count = 0
        self._interrupted = asyncio.Event()
        self._agent_audio_bytes = 0
        return self

    @property
    def interruption_count(self) -> int:
        return self._interruption_count

    @property
    def agent_audio_bytes(self) -> int:
        return self._agent_audio_bytes

    async def play(self, pcm: bytes) -> None:
        """Count agent audio; nothing is rendered on this host."""
        self._agent_audio_bytes += len(pcm)

    async def flush(self) -> None:
        """Barge-in observed: the session flushes the sink on interruption."""
        self._interruption_count += 1
        self._interrupted.set()

    def start(self, transport: ChunkTransport) -> None:
        """Begin the continuous real-time audio pump into the transport."""
        self._pump = asyncio.create_task(self._pump_loop(transport))

    async def stop(self) -> None:
        if self._pump is None:
            return
        self._pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._pump

    def speak(self, pcm: bytes) -> None:
        """Queue one utterance; the pump streams it at real-time pace."""
        for offset in range(0, len(pcm), _CHUNK_BYTES):
            chunk = pcm[offset : offset + _CHUNK_BYTES]
            # Pad the tail chunk so every send stays one 64ms frame.
            self._pending.append(chunk.ljust(_CHUNK_BYTES, b"\x00"))

    async def wait_spoken(self, timeout_s: float = 60.0) -> None:
        """Return once every queued utterance chunk has been streamed.

        Raises when the pump is dead with chunks still pending -- waiting
        on a queue nobody drains would otherwise block a billed run until
        its outer cap (or a dry run forever).
        """
        deadline = time.monotonic() + timeout_s
        while self._pending:
            if self._pump is None or self._pump.done():
                msg = f"audio pump stopped with {len(self._pending)} chunks pending"
                raise RuntimeError(msg)
            if time.monotonic() > deadline:
                msg = f"utterance not drained within {timeout_s}s"
                raise TimeoutError(msg)
            await asyncio.sleep(_CHUNK_SECONDS)

    async def wait_interrupted(self, timeout_s: float) -> bool:
        """Wait for the first barge-in; report whether one was observed."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout_s):
                await self._interrupted.wait()
        return self._interrupted.is_set()

    async def _pump_loop(self, transport: ChunkTransport) -> None:
        try:
            while True:
                chunk = self._pending.popleft() if self._pending else _SILENCE
                await transport.send_audio_chunk(chunk)
                await asyncio.sleep(_CHUNK_SECONDS)
        except ConnectionClosed as exc:
            # The socket died under the pump: stamp it -- the adjudicator
            # reads a dead session as a survival failure, not a crash.
            self._trace.record("note", "mic_pump_stopped", {"error": str(exc)})


@final
class BargeInFlow:
    """Drive the scripted barge-in conversation over one open session."""

    _session: ConvAISession
    _mic: SyntheticAudio
    _trace: EventTrace
    _utterances: BargeInUtterances

    def __new__(
        cls,
        *,
        session: ConvAISession,
        mic: SyntheticAudio,
        trace: EventTrace,
        utterances: BargeInUtterances,
    ) -> Self:
        self = super().__new__(cls)
        self._session = session
        self._mic = mic
        self._trace = trace
        self._utterances = utterances
        return self

    async def run(self) -> None:
        """Execute the script; every step lands in the trace as evidence."""
        self._mic.start(self._session)
        try:
            await self._await_condition(
                lambda: self._agent_replies() >= 1, 12.0, "greeting"
            )
            # The agent_response event outruns its audio by ~2s; speaking
            # over the greeting got the first utterance clipped by ASR.
            await asyncio.sleep(3.0)
            if not await self._trigger_slow_tool():
                return  # no tool call: the adjudicator rules INCONCLUSIVE
            await self._interrupt_mid_call()
            await self._probe_recall()
            await self._note_roundtrip()
        finally:
            await self._mic.stop()

    # -- Steps ---------------------------------------------------------------

    async def _trigger_slow_tool(self) -> bool:
        await self._speak("trigger_search", TRIGGER_TEXT, self._utterances.trigger)
        return await self._await_condition(
            self._search_started, 30.0, "search_code client_tool_call"
        )

    async def _interrupt_mid_call(self) -> None:
        # The slow tool holds the call open 2-5s; a short beat puts the
        # interruption unambiguously inside that window.
        await asyncio.sleep(0.2)
        await self._speak("interrupt", INTERRUPT_TEXT, self._utterances.interrupt)
        observed = await self._mic.wait_interrupted(12.0)
        self._trace.record(
            "note",
            "interrupt_observed",
            {"interrupted": observed, "count": self._mic.interruption_count},
        )
        await self._await_condition(self._search_settled, 25.0, "post-interrupt settle")
        await asyncio.sleep(2.5)  # let trailing agent events land in the trace

    async def _probe_recall(self) -> None:
        replies_before = self._agent_replies()
        await self._speak("probe_recall", PROBE_TEXT, self._utterances.probe)
        await self._await_condition(
            lambda: self._agent_replies() > replies_before, 30.0, "recall answer"
        )

    async def _note_roundtrip(self) -> None:
        await self._speak("note_roundtrip", NOTE_TEXT, self._utterances.note)
        await self._await_condition(self._note_done, 30.0, "write_note result")
        await asyncio.sleep(2.5)  # capture the agent's acknowledgment

    # -- Session observation ---------------------------------------------------

    def _agent_replies(self) -> int:
        transcript = self._session.metrics.transcript
        return sum(1 for entry in transcript if entry["role"] == "agent")

    def _search_started(self) -> bool:
        invocations = self._session.metrics.invocations
        return any(inv.tool_name == "search_code" for inv in invocations)

    def _search_settled(self) -> bool:
        searches = [
            inv
            for inv in self._session.metrics.invocations
            if inv.tool_name == "search_code"
        ]
        return bool(searches) and all(inv.t_result is not None for inv in searches)

    def _note_done(self) -> bool:
        return any(
            inv.tool_name == "write_note" and inv.t_result is not None
            for inv in self._session.metrics.invocations
        )

    async def _speak(self, step: str, text: str, pcm: bytes) -> None:
        self._trace.record("note", "barge_in_step", {"step": step, "text": text})
        self._mic.speak(pcm)
        await self._mic.wait_spoken()

    async def _await_condition(
        self, done: Callable[[], bool], timeout_s: float, label: str
    ) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if done():
                return True
            await asyncio.sleep(0.05)
        self._trace.record("note", "flow_timeout", {"waiting_for": label})
        return False
