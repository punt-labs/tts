"""``vox call`` -- a live voice call with the user's active Claude Code session.

Wires :class:`~punt_vox.voxd.conversation_mode.call_session.CallSession` (the
tested orchestration in ``voxd/conversation_mode/``) to the CLI: session
discovery (:class:`~.session_discovery.SessionDiscovery`), the real
session-attach mechanism (:class:`~.claude_session_attach.ClaudeSessionAttach`),
the UserPromptSubmit lock (:class:`~.call_lock.CallLock`), cross-process
control (:class:`~.call_control.CallControl`), and speech through the
existing daemon client (:class:`VoxClientSync.synthesize`).

**Deferred to a follow-up mission**, per this mission's write-set boundary
(``src/punt_vox/server.py`` and ``src/punt_vox/providers/`` are locked by
another open mission): the ``mic:call`` MCP tool, and the real
ElevenLabs-backed :class:`~punt_vox.voxd.conversation_mode.stt_provider.STTProvider`.
Live microphone capture is deferred alongside the real STT provider for the
same reason a fake STT provider would have nothing genuine to transcribe --
``vox call start`` instead reads a JSON Lines script of scripted utterances
(``--script``), builds synthetic speech/silence :class:`AudioChunk` values
sized to close a turn through the *real* :class:`TurnDetector`, and feeds a
:class:`FakeSTTProvider`-equivalent seeded from the same script. Every other
component in the pipeline -- the detector, the call state machine, session
discovery, ``ClaudeSessionAttach``, the audible cues -- is the real
implementation.
"""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self, final

import typer

from punt_vox.client_sync import VoxClientSync
from punt_vox.dirs import DEFAULT_CONFIG_DIR, find_repo_root
from punt_vox.voxd.conversation_mode.audio_chunk import AudioChunk
from punt_vox.voxd.conversation_mode.call_control import CallControl
from punt_vox.voxd.conversation_mode.call_lock import CallLock
from punt_vox.voxd.conversation_mode.call_session import CallSession
from punt_vox.voxd.conversation_mode.claude_session_attach import ClaudeSessionAttach
from punt_vox.voxd.conversation_mode.session_discovery import SessionDiscovery
from punt_vox.voxd.conversation_mode.stt_provider import TranscriptEvent
from punt_vox.voxd.conversation_mode.turn_detector import TurnDetector

if TYPE_CHECKING:
    from punt_vox.types import HealthCheck
    from punt_vox.voxd.conversation_mode.session_attach import SessionAttach

__all__ = ["build_call_app"]

_CHUNK_S = 0.02
_SPEECH_CHUNKS = 20  # 400ms of synthetic "speech" per scripted utterance
_SILENCE_CHUNKS = 10  # 200ms of synthetic silence to close the turn


@final
class ScriptedTurn:
    """One line of a ``--script`` file: a scripted utterance and its confidence."""

    __slots__ = ("_confidence", "_text")
    _text: str
    _confidence: float

    def __new__(cls, text: str, confidence: float) -> Self:
        self = super().__new__(cls)
        self._text = text
        self._confidence = confidence
        return self

    @property
    def text(self) -> str:
        return self._text

    @property
    def confidence(self) -> float:
        return self._confidence

    @staticmethod
    def _pcm(amplitude: int, sample_count: int = 320) -> bytes:
        return struct.pack(f"<{sample_count}h", *([amplitude] * sample_count))

    @classmethod
    def silence_chunks(cls, count: int) -> list[AudioChunk]:
        """Return ``count`` chunks of pure silence, for calibration floors."""
        return [AudioChunk(pcm=cls._pcm(0), duration_s=_CHUNK_S) for _ in range(count)]

    @classmethod
    def read_script(cls, path: Path) -> list[ScriptedTurn]:
        """Parse a JSON Lines file of ``{"text": ..., "confidence": ...}`` entries."""
        turns: list[ScriptedTurn] = []
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            payload = json.loads(stripped)
            turns.append(cls(text=payload["text"], confidence=payload["confidence"]))
        return turns

    def synthetic_chunks(self) -> list[AudioChunk]:
        """Return synthetic speech-then-silence chunks that close one turn.

        Stands in for real microphone capture (deferred alongside the real
        STT provider): enough amplitude to cross the calibrated audible
        threshold, for long enough to satisfy ``min_speech_s``, followed by
        a genuine silence gap so the real :class:`TurnDetector` closes the
        run on its own logic rather than a shortcut.
        """
        speech = [
            AudioChunk(pcm=self._pcm(20000), duration_s=_CHUNK_S)
        ] * _SPEECH_CHUNKS
        silence = [AudioChunk(pcm=self._pcm(0), duration_s=_CHUNK_S)] * _SILENCE_CHUNKS
        return [*speech, *silence]


@final
class ScriptedSTTProvider:
    """An :class:`STTProvider` seeded by :class:`ScriptedTurn` entries, one at a time.

    Consumed in the same order the CLI feeds :class:`ScriptedTurn` chunks
    through :class:`CallSession`, so the Nth turn's chunks are always
    transcribed against the Nth script line -- the substitute for a real STT
    provider until ``src/punt_vox/providers/`` is unblocked.
    """

    __slots__ = ("_index", "_turns")
    _turns: list[ScriptedTurn]
    _index: int

    def __new__(cls, turns: list[ScriptedTurn]) -> Self:
        self = super().__new__(cls)
        self._turns = turns
        self._index = 0
        return self

    @property
    def name(self) -> str:
        return "scripted"

    async def transcribe(
        self, chunks: AsyncIterator[AudioChunk]
    ) -> AsyncIterator[TranscriptEvent]:
        async for _chunk in chunks:
            pass
        turn = self._turns[self._index]
        self._index += 1
        yield TranscriptEvent(text=turn.text, confidence=turn.confidence, is_final=True)

    def check_health(self) -> list[HealthCheck]:
        return []


_ScriptOpt = Annotated[
    Path,
    typer.Option(
        "--script",
        help=(
            "JSON Lines file of scripted turns "
            '({"text": ..., "confidence": ...} per line). Live microphone '
            "capture is deferred to a follow-up mission alongside the real "
            "ElevenLabs STT provider."
        ),
    ),
]
_SessionOpt = Annotated[
    str | None,
    typer.Option(
        "--session", help="Attach to this session id instead of discovering one."
    ),
]
_TransferSessionOpt = Annotated[
    str | None,
    typer.Option(
        "--session", help="Re-attach to this session id, or re-discover if omitted."
    ),
]


@final
class CallCli:
    """The three ``vox call`` verbs, as bound methods registered on a Typer app.

    Methods, not decorator-wrapped local functions, so each is a real,
    referenced attribute -- avoiding the "defined but never read" false
    positive a locally-scoped ``@app.command`` closure triggers, the same
    shape :class:`~punt_vox.cli_rec.RecCli` already uses for ``vox rec``.
    """

    __slots__ = ()

    def __new__(cls) -> Self:
        return super().__new__(cls)

    def start(self, script: _ScriptOpt, session: _SessionOpt = None) -> None:
        """Start a call: listen, detect turns, forward them, speak the reply."""
        asyncio.run(self._run(script, session))

    def stop(self) -> None:
        """Ask the running call to hang up (FR-2's explicit end)."""
        CallControl(self._lock_dir() / "call.control").request_stop()

    def transfer(self, session: _TransferSessionOpt = None) -> None:
        """Ask the running call to re-attach to a different active session."""
        CallControl(self._lock_dir() / "call.control").request_transfer(session)

    @staticmethod
    def _lock_dir() -> Path:
        root = find_repo_root() or Path.cwd()
        return root / DEFAULT_CONFIG_DIR / "call"

    @staticmethod
    async def _resolve_session_attach(
        cwd: Path, session_id: str | None
    ) -> SessionAttach:
        """Resolve which session to attach, per the ADR's no-silent-auto-pick rule."""
        if session_id is not None:
            return ClaudeSessionAttach(session_id=session_id)
        candidates = await SessionDiscovery().discover(cwd)
        if len(candidates) == 1:
            return ClaudeSessionAttach(session_id=candidates[0].session_id)
        if not candidates:
            msg = (
                "no active Claude Code session found for this directory; "
                "start a session first, or pass --session <id>"
            )
            raise typer.BadParameter(msg)
        listing = "\n".join(f"  {c.session_id}  ({c.cwd})" for c in candidates)
        msg = (
            "multiple active Claude Code sessions found; pass --session "
            f"<id> to choose one:\n{listing}"
        )
        raise typer.BadParameter(msg)

    @staticmethod
    def _calibrated_detector() -> TurnDetector:
        """Return a :class:`TurnDetector` calibrated against a synthetic silent floor.

        Stands in for FR-1's "a few seconds of 'say something'" live calibration
        step, deferred alongside real capture -- the synthetic floor is silence
        (amplitude 0), matching :meth:`ScriptedTurn.synthetic_chunks`'s own
        silence chunks, so the real detector's thresholds are meaningful against
        the synthetic audio this CLI path feeds it.
        """
        detector = TurnDetector()
        detector.calibrate(ScriptedTurn.silence_chunks(10))
        return detector

    async def _run(self, script: Path, session_id: str | None) -> None:
        turns = ScriptedTurn.read_script(script)
        client = VoxClientSync()

        def speak(text: str) -> None:
            # Discards SynthesizeResult -- SpeakFn's contract is "spoken", not "the
            # daemon's synthesis metadata", and a call orchestrator has no use for it.
            client.synthesize(text)

        lock = CallLock(self._lock_dir() / "call.lock")
        control = CallControl(self._lock_dir() / "call.control")
        session_attach = await self._resolve_session_attach(Path.cwd(), session_id)
        session = CallSession(
            turn_detector=self._calibrated_detector(),
            stt_provider=ScriptedSTTProvider(turns),
            session_attach=session_attach,
            speak=speak,
        )
        lock.acquire("conversation mode call active")
        try:
            await session.start()
            for turn in turns:
                request = control.consume()
                if request is not None and request.kind == "stop":
                    break
                if request is not None and request.kind == "transfer":
                    await self._resolve_session_attach(
                        Path.cwd(), request.target_session_id
                    )
                for chunk in turn.synthetic_chunks():
                    await session.process_chunk(chunk)
            await session.hangup()
        finally:
            lock.release()


def build_call_app() -> typer.Typer:
    """Return the ``vox call`` Typer group with bound methods (no wrappers)."""
    cli = CallCli()
    app = typer.Typer(
        help="A live voice call with the user's active Claude Code session.",
        no_args_is_help=True,
    )
    app.command("start")(cli.start)
    app.command("stop")(cli.stop)
    app.command("transfer")(cli.transfer)
    return app
