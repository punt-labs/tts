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

**Known limitation: the mic-echo guard is duration-estimated, not
signal-based.** ``VoxClientSync.synthesize`` (via ``client.py``'s
``send_and_drain(..., early_terminal="playing")``) returns as soon as voxd
reports audio *enqueued*, not when it finishes playing -- ``client.py``
does not expose a true playback-completion signal, and extending it to do
so is out of this slice's write-set (it is locked by a separate, still-open
mission). :class:`~.call_live_driver.LiveCallDriver` closes the microphone
gate before speaking and holds it shut for an *estimated* speech duration
plus a safety margin, not the real one -- see
:func:`~punt_vox.providers.convert.estimate_speech_duration_s`. This bounds
the echo window; it does not eliminate it. A real fix needs
either ``client.py`` to expose a genuine "wait for playback done" option, or
routing playback through :class:`~.playback_sink_actor.PlaybackSinkActor`
(built in an earlier slice but not yet wired to any caller) -- both are
follow-up work, not attempted here. The estimate also assumes playback
starts immediately once ``synthesize()`` returns; if the daemon queues the
utterance behind music or a chime, the gate can reopen while the reply is
still actually playing.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

import typer

from punt_vox.client_sync import VoxClientSync
from punt_vox.commands.call_cues import DaemonCues
from punt_vox.commands.call_live_driver import LiveCallDriver
from punt_vox.commands.call_options import (
    ScriptOpt as _ScriptOpt,
    SessionOpt as _SessionOpt,
    TraceTurnsOpt as _TraceTurnsOpt,
    TransferSessionOpt as _TransferSessionOpt,
)
from punt_vox.commands.call_scripted import ScriptedSTTProvider, ScriptedTurn
from punt_vox.commands.call_spec import resolve_call_spec
from punt_vox.dirs import DEFAULT_CONFIG_DIR, find_repo_root
from punt_vox.logging_config import configure_turn_timer_logging
from punt_vox.voxd.conversation_mode.call_control import CallControl
from punt_vox.voxd.conversation_mode.call_lock import CallLock, CallLockActiveError
from punt_vox.voxd.conversation_mode.call_session import CallSession, SpeakFn
from punt_vox.voxd.conversation_mode.claude_session_attach import ClaudeSessionAttach
from punt_vox.voxd.conversation_mode.session_discovery import SessionDiscovery
from punt_vox.voxd.conversation_mode.turn_detector import TurnDetector

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.session_attach import SessionAttach
    from punt_vox.voxd.conversation_mode.wait_cue import ChimeFn

__all__ = ["build_call_app"]

logger = logging.getLogger(__name__)


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

    def start(
        self,
        script: _ScriptOpt = None,
        session: _SessionOpt = None,
        trace_turns: _TraceTurnsOpt = False,  # noqa: FBT002 -- typer CLI requires bool default
    ) -> None:
        """Start a call: listen, detect turns, forward them, speak the reply."""
        # Always on, regardless of *trace_turns*: the turn-timer's own
        # logger is forced to DEBUG on vox.log for every call, not only
        # ones run with --trace-turns -- see configure_turn_timer_logging's
        # docstring. trace_turns only decides whether those same lines also
        # echo to this terminal.
        configure_turn_timer_logging(echo_to_console=trace_turns)
        asyncio.run(self._run(script, session))

    def stop(self) -> None:
        """Ask the running call to hang up (FR-2's explicit end).

        Reduces, but does not eliminate, the stale-mailbox race: refuses to
        write a request when :meth:`_require_live_call` finds no call
        currently live, so a stop (or transfer) written against an
        already-dead lock no longer sits in the mailbox for the *next* call
        to silently consume on its first chunk. The check and the write are
        two separate filesystem operations in this process, uncoordinated
        with the live call's own ``lock.release()`` in a different process
        -- if the live call ends in the narrow window between them, the
        request still gets written and still lands in the next call's
        mailbox. A full fix needs the request tagged (pid/timestamp) and
        validated again on :meth:`~.call_control.CallControl.consume`.
        """
        self._require_live_call()
        CallControl(self._lock_dir() / "call.control").request_stop()

    def transfer(self, session: _TransferSessionOpt = None) -> None:
        """Ask the running call to re-attach to a different active session.

        Same reduces-but-does-not-eliminate stale-mailbox race as
        :meth:`stop` -- see that docstring.
        """
        self._require_live_call()
        CallControl(self._lock_dir() / "call.control").request_transfer(session)

    def _require_live_call(self) -> None:
        # is_live(), not read() is not None: a lock file can outlive the
        # process that wrote it (a killed/crashed vox call start leaves the
        # file behind with a now-dead pid). A stop/transfer against that
        # stale file must refuse the same as "no call is active" -- writing
        # one anyway lands in a mailbox nobody will ever read, since the
        # process it thinks it is stopping is long gone.
        if not CallLock(self._lock_dir() / "call.lock").is_live():
            msg = "no call is active"
            raise typer.BadParameter(msg)

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
        # Resolved once, before the call state machine starts -- see
        # call_spec.py's module docstring for why: a call that cannot
        # speak must refuse to start, not begin and then have every
        # speak() call fail silently on the wire.
        cues = DaemonCues(client, resolve_call_spec())
        speak = cues.speak
        chime = cues.chime

        lock = CallLock(self._lock_dir() / "call.lock")
        control = CallControl(self._lock_dir() / "call.control")
        session_attach = await self._resolve_session_attach(Path.cwd(), session_id)

        try:
            lock.acquire("conversation mode call active")
        except CallLockActiveError as exc:
            raise typer.BadParameter(str(exc)) from exc

        try:
            if script is not None:
                await self._run_scripted(script, session_attach, speak, chime, control)
            else:
                driver = await LiveCallDriver.create(
                    session_attach=session_attach,
                    speak=speak,
                    chime=chime,
                    control=control,
                    apply_control=self._apply_control,
                )
                await driver.run()
        except Exception as exc:
            # System boundary (PY-EH-6): the CLI entry point for a live call.
            # Without this, a provider fault, a subprocess failure, or a mic
            # device-open error dies as a bare traceback with total silence to
            # the human on the other end of the call -- the terminal is not
            # where they are looking. Logs the full exception for diagnosis,
            # speaks a short summary, then re-raises so the process still
            # exits non-zero and the traceback remains available.
            logger.exception("call ended unexpectedly")
            try:
                await speak(
                    f"The call ended unexpectedly: {exc}. "
                    "Check the terminal for details."
                )
            except Exception:
                # speak() is itself a daemon RPC that can fail for the same
                # root cause the call just died from -- Python's implicit
                # exception chaining would otherwise let THIS failure
                # replace *exc* as what propagates from this handler, and
                # the finally block below would release the lock against
                # the wrong exception context. Logged as a secondary
                # failure, never re-raised: the original *exc* is what the
                # human's terminal and this process's exit code must show.
                logger.exception("also failed to speak the call-ended summary")
            raise
        finally:
            lock.release()

    async def _run_scripted(
        self,
        script: Path,
        session_attach: SessionAttach,
        speak: SpeakFn,
        chime: ChimeFn,
        control: CallControl,
    ) -> None:
        """Drive one call from a JSON Lines script -- no microphone, no ElevenLabs."""
        turns = ScriptedTurn.read_script(script)
        session = CallSession(
            turn_detector=self._calibrated_detector(),
            stt_provider=ScriptedSTTProvider(turns),
            session_attach=session_attach,
            speak=speak,
            chime=chime,
        )
        await session.start()
        for turn in turns:
            if await self._apply_control(control, session, speak):
                break
            for chunk in turn.synthetic_chunks():
                await session.process_chunk(chunk)
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
