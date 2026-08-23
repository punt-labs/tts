"""``vox call`` -- a live voice call with the user's active Claude Code session.

Wires :class:`~punt_vox.voxd.conversation_mode.call_session.CallSession` (the
tested orchestration in ``voxd/conversation_mode/``) to the CLI: session
discovery (:class:`~.session_discovery.SessionDiscovery`), the real
session-attach mechanism (:class:`~.claude_session_attach.ClaudeSessionAttach`),
the UserPromptSubmit lock (:class:`~.call_lock.CallLock`), cross-process
control (:class:`~.call_control.CallControl`), and speech through the
existing daemon client (:class:`VoxClientSync.synthesize`).

Two ways to drive one call, chosen at ``vox call start`` by whether
``--script`` is passed:

- **Live (default).** :class:`~.mic_audio_source.MicAudioSource` captures
  the real microphone; :class:`~punt_vox.providers.elevenlabs_stt.ElevenLabsSTTProvider`
  transcribes each closed turn. This is the path a human uses.
- **Scripted (``--script``, dev/test).** A JSON Lines file of pre-written
  utterances drives synthetic speech/silence :class:`AudioChunk` values
  through the same *real* :class:`TurnDetector`, transcribed by
  :class:`ScriptedSTTProvider` reading the same script -- no microphone, no
  ElevenLabs credentials, no network. Existing for demos and CI, not the
  primary way to place a call.

Every other component in the pipeline -- the detector, the call state
machine, session discovery, ``ClaudeSessionAttach``, the audible cues -- is
shared between both paths, unchanged by which one is chosen.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self, final

import typer

from punt_vox.client_sync import VoxClientSync
from punt_vox.commands.call_scripted import ScriptedSTTProvider, ScriptedTurn
from punt_vox.dirs import DEFAULT_CONFIG_DIR, find_repo_root
from punt_vox.providers.elevenlabs_stt import ElevenLabsSTTProvider
from punt_vox.voxd.conversation_mode.call_control import CallControl
from punt_vox.voxd.conversation_mode.call_lock import CallLock, CallLockActiveError
from punt_vox.voxd.conversation_mode.call_session import CallSession, SpeakFn
from punt_vox.voxd.conversation_mode.claude_session_attach import ClaudeSessionAttach
from punt_vox.voxd.conversation_mode.mic_audio_source import MicAudioSource
from punt_vox.voxd.conversation_mode.session_discovery import SessionDiscovery
from punt_vox.voxd.conversation_mode.turn_detector import TurnDetector

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.session_attach import SessionAttach

__all__ = ["build_call_app"]

logger = logging.getLogger(__name__)

# FR-1's "a few seconds of 'say something'" calibration step, for the live
# path: how long to sample ambient microphone audio before the call opens
# for real speech, so :class:`TurnDetector`'s noise floor reflects the
# room the human is actually calling from.
_CALIBRATION_S = 2.0


_ScriptOpt = Annotated[
    Path | None,
    typer.Option(
        "--script",
        help=(
            "Dev/test path: a JSON Lines file of scripted turns "
            '({"text": ..., "confidence": ...} per line), fed through '
            "synthetic audio instead of the microphone -- no hardware, no "
            "ElevenLabs credentials, no network. Omit for a real call: real "
            "microphone capture, transcribed by ElevenLabs."
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

    def start(self, script: _ScriptOpt = None, session: _SessionOpt = None) -> None:
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

        Used only by the scripted path: the synthetic floor is silence
        (amplitude 0), matching :meth:`ScriptedTurn.synthetic_chunks`'s own
        silence chunks, so the real detector's thresholds are meaningful
        against the synthetic audio that path feeds it. The live path
        calibrates against real ambient audio instead -- see
        :meth:`_run_live`.
        """
        detector = TurnDetector()
        detector.calibrate(ScriptedTurn.silence_chunks(10))
        return detector

    async def _run(self, script: Path | None, session_id: str | None) -> None:
        """Dispatch to the scripted or live path, per whether *script* is set."""
        client = VoxClientSync()

        async def speak(text: str) -> None:
            # asyncio.to_thread, not a bare call: VoxClientSync.synthesize blocks
            # its calling thread for the full round trip to the daemon, and this
            # runs on the call's single event loop -- a blocking call here would
            # stall audio capture and control-request handling for as long as
            # synthesis takes. Discards SynthesizeResult -- SpeakFn's contract is
            # "spoken", not "the daemon's synthesis metadata", and a call
            # orchestrator has no use for it.
            await asyncio.to_thread(client.synthesize, text)

        lock = CallLock(self._lock_dir() / "call.lock")
        control = CallControl(self._lock_dir() / "call.control")
        session_attach = await self._resolve_session_attach(Path.cwd(), session_id)

        try:
            lock.acquire("conversation mode call active")
        except CallLockActiveError as exc:
            raise typer.BadParameter(str(exc)) from exc

        try:
            if script is not None:
                await self._run_scripted(script, session_attach, speak, control)
            else:
                await self._run_live(session_attach, speak, control)
        except Exception as exc:
            # System boundary (PY-EH-6): the CLI entry point for a live call.
            # Without this, a provider fault, a subprocess failure, or a mic
            # device-open error dies as a bare traceback with total silence to
            # the human on the other end of the call -- the terminal is not
            # where they are looking. Logs the full exception for diagnosis,
            # speaks a short summary, then re-raises so the process still
            # exits non-zero and the traceback remains available.
            logger.exception("call ended unexpectedly")
            await speak(
                f"The call ended unexpectedly: {exc}. Check the terminal for details."
            )
            raise
        finally:
            lock.release()

    async def _run_scripted(
        self,
        script: Path,
        session_attach: SessionAttach,
        speak: SpeakFn,
        control: CallControl,
    ) -> None:
        """Drive one call from a JSON Lines script -- no microphone, no ElevenLabs."""
        turns = ScriptedTurn.read_script(script)
        session = CallSession(
            turn_detector=self._calibrated_detector(),
            stt_provider=ScriptedSTTProvider(turns),
            session_attach=session_attach,
            speak=speak,
        )
        await session.start()
        for turn in turns:
            if await self._apply_control(control, session, speak):
                break
            for chunk in turn.synthetic_chunks():
                await session.process_chunk(chunk)
        await session.hangup()

    async def _run_live(
        self,
        session_attach: SessionAttach,
        speak: SpeakFn,
        control: CallControl,
    ) -> None:
        """Drive one call from the real microphone, transcribed by ElevenLabs."""
        mic_source = MicAudioSource()
        detector = TurnDetector()
        detector.calibrate(await mic_source.capture_seconds(_CALIBRATION_S))

        async def _speak_and_drain(text: str) -> None:
            # The microphone keeps capturing while ANY cue plays through the
            # speakers and picks it back up -- not only the reply itself.
            # "Ready." is spoken after the speaking -> listening transition
            # has already fired, and the ask-to-repeat cue has no transition
            # around it at all (the call stays in listening throughout), so
            # a drain tied to the mode transition alone misses both.
            # CallSession.process_chunk already refuses to feed the turn
            # detector while speaking, but nothing stops that self-captured
            # audio from queuing up in mic_source's own buffer meanwhile;
            # draining unconditionally after every utterance -- the one
            # thing every cue has in common -- is what actually clears it,
            # regardless of which mode the call is in when the utterance
            # finishes.
            await speak(text)
            mic_source.drain_pending()

        session = CallSession(
            turn_detector=detector,
            stt_provider=ElevenLabsSTTProvider(),
            session_attach=session_attach,
            speak=_speak_and_drain,
        )

        await session.start()
        chunks = mic_source.chunks()
        try:
            async for chunk in chunks:
                if await self._apply_control(control, session, _speak_and_drain):
                    break
                await session.process_chunk(chunk)
        finally:
            await chunks.aclose()
        await session.hangup()

    async def _apply_control(
        self, control: CallControl, session: CallSession, speak: SpeakFn
    ) -> bool:
        """Consume one pending control request; return whether the loop should stop.

        Shared by both drive loops so a stop/transfer request is handled
        identically whether the audio is scripted or live. A transfer
        request re-resolves the session attach and feeds it into the
        already-running *session* via
        :meth:`~.call_session.CallSession.replace_session_attach`, so
        ``/call transfer`` redirects the call without ending it -- including
        when the resolution itself fails: zero or multiple discoverable
        sessions raises ``typer.BadParameter``, and letting that escape
        would hit the outer boundary handler and end the whole call over
        what is only an invalid transfer request. Caught here instead, so
        the human hears why the transfer didn't happen and the call
        continues on its current session.
        """
        request = control.consume()
        if request is None:
            return False
        if request.kind == "stop":
            return True
        if request.kind == "transfer":
            try:
                new_attach = await self._resolve_session_attach(
                    Path.cwd(), request.target_session_id
                )
            except typer.BadParameter as exc:
                await speak(f"Couldn't transfer the call: {exc}")
                return False
            session.replace_session_attach(new_attach)
        return False


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
