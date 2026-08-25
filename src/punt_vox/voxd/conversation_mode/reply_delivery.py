"""Deliver one turn's reply: session-attach exchange, redaction, and speech.

Extracted out of :class:`~.call_session.CallSession` (PY-OO-2/PY-RF-3 split
module -- ``_speak_reply`` and its three collaborators, :class:`WaitCue`,
:class:`ReplyRecovery`, and the session-attach reference, were used nowhere
else in that class) so the orchestration of one reply's ack cue, wait chime,
transcript exchange, redaction, and speech lives in one place with its own
module budget.
"""

from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING, Self, final

from punt_vox.quips import CALL_ACK_PHRASES
from punt_vox.voxd.conversation_mode.reply_begins import ReplyBegins
from punt_vox.voxd.conversation_mode.reply_ends import ReplyEnds
from punt_vox.voxd.conversation_mode.reply_recovery import ReplyRecovery
from punt_vox.voxd.conversation_mode.reply_redaction import reply_redactor
from punt_vox.voxd.conversation_mode.session_attach import SessionAttachError
from punt_vox.voxd.conversation_mode.wait_cue import WaitCue

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.call_actor import CallActor
    from punt_vox.voxd.conversation_mode.session_attach import SessionAttach
    from punt_vox.voxd.conversation_mode.speak_fn import SpeakFn
    from punt_vox.voxd.conversation_mode.turn import TranscribedTurn
    from punt_vox.voxd.conversation_mode.turn_timer import TurnTimer
    from punt_vox.voxd.conversation_mode.wait_cue import ChimeFn

__all__ = ["ReplyDelivery"]

logger = logging.getLogger(__name__)


@final
class ReplyDelivery:
    """Run one turn through session-attach and speak the (redacted) reply."""

    __slots__ = (
        "_actor",
        "_reply_recovery",
        "_session_attach",
        "_speak",
        "_turn_timer",
        "_wait_cue",
    )
    _actor: CallActor
    _reply_recovery: ReplyRecovery
    _session_attach: SessionAttach
    _speak: SpeakFn
    _turn_timer: TurnTimer
    _wait_cue: WaitCue

    def __new__(
        cls,
        *,
        session_attach: SessionAttach,
        speak: SpeakFn,
        actor: CallActor,
        turn_timer: TurnTimer,
        chime: ChimeFn | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._session_attach = session_attach
        self._speak = speak
        self._actor = actor
        self._turn_timer = turn_timer
        self._reply_recovery = ReplyRecovery(actor, speak)
        self._wait_cue = WaitCue(chime)
        return self

    def replace_session_attach(self, session_attach: SessionAttach) -> None:
        """Re-attach to a different session (``/call transfer``).

        Takes effect on the next :meth:`deliver` call -- a turn already
        mid-flight when a transfer request lands keeps talking to the
        session it started with.
        """
        self._session_attach = session_attach

    async def deliver(self, turn: TranscribedTurn) -> None:
        """Run *turn* through session-attach and speak the (redacted) reply."""
        # The per-turn claude subprocess spawn measured 13-25s median -- an
        # instant acknowledgment covers the human's first few seconds of
        # otherwise-dead silence, before the STT->claude->TTS round trip
        # even starts. Goes through self._speak, the same mic-gated channel
        # every other cue in this flow uses, so it is never captured as if
        # the human said it.
        await self._speak(secrets.choice(CALL_ACK_PHRASES))
        self._turn_timer.mark("ack_spoken")

        reply_text = await self._exchange(turn)
        if reply_text is None:
            return  # ReplyRecovery already spoke and ended/recovered the call

        self._turn_timer.mark("reply_complete")
        self._actor.apply(ReplyBegins())
        self._turn_timer.mark("tts_request_sent")
        # Full text to vox.log (0600); only the redacted text is ever spoken
        # -- see reply_redactor's own docstring for the threat this guards.
        logger.info("call reply (unredacted, vox.log only): %s", reply_text)
        await self._speak(reply_redactor(reply_text))
        # "playback_started" is the best available proxy, not a real signal:
        # SpeakFn's own contract (see that Protocol's docstring) is "returns
        # once playback has started OR completed" -- this class cannot tell
        # which, and a caller wrapping speak() in its own timing-sensitive
        # logic (the live path's mic-echo gate) can push this mark later
        # still, past when audio actually started.
        self._turn_timer.mark(
            "playback_started", detail="approximate -- see SpeakFn's contract"
        )
        # ReplyEnds() returns the call to LISTENING, and process_chunk()
        # accepts turn-detector input in both LISTENING and WAITING -- apply
        # it only after "Ready." finishes, or the cue itself risks being
        # treated as human speech by a SpeakFn that doesn't gate the mic.
        await self._speak("Ready.")
        self._actor.apply(ReplyEnds())

    async def _exchange(self, turn: TranscribedTurn) -> str | None:
        """Run session-attach for *turn*, returning the assembled reply text.

        Returns ``None`` after a ``SessionAttachError`` is already handled by
        :class:`ReplyRecovery` (which speaks its own recovery message and, on
        a permanent failure, ends the call itself) -- :meth:`deliver` treats
        that as nothing left to do for this turn.
        """
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
            return None
        return "".join(pieces)
