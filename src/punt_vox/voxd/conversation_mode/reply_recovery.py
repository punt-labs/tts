"""Recovers a call from a mid-turn session-attach failure, per exception subtype.

Two failures reach :class:`~.call_session.CallSession` through the same
:class:`~.session_attach.SessionAttachError` hierarchy, but only one of them
can succeed on retry: a subprocess crash, a malformed reply, or a timeout is
transient, so the call recovers to listening and the human just tries again.
A missing ``ANTHROPIC_API_KEY`` is certain and permanent -- every subsequent
turn would hit exactly the same failure -- so that one ends the call instead
of looping "try again" forever.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd.conversation_mode.end_call import EndCall
from punt_vox.voxd.conversation_mode.reply_begins import ReplyBegins
from punt_vox.voxd.conversation_mode.reply_ends import ReplyEnds
from punt_vox.voxd.conversation_mode.session_attach import BareAuthMissingError

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.call_actor import CallActor
    from punt_vox.voxd.conversation_mode.session_attach import SessionAttachError
    from punt_vox.voxd.conversation_mode.speak_fn import SpeakFn

__all__ = ["ReplyRecovery"]

logger = logging.getLogger(__name__)

# FR-19's low-confidence path never leaves listening for the turn it can't
# act on; a SessionAttachError happens after TurnDetected already moved the
# call to waiting (see CallState.turn_detected's precondition), so recovery
# has to speak its way back through speaking -> listening rather than
# staying put -- the apology IS the reply this turn gets.
_SESSION_ATTACH_FAILED = "Sorry, something went wrong -- go ahead and try again."

# Unlike _SESSION_ATTACH_FAILED, this ends the call outright rather than
# recovering to listening, so the sentence says so instead of inviting a
# doomed retry.
_BARE_AUTH_MISSING = (
    "Sorry, this call can't continue -- the API key it needs to reach your "
    "session isn't set. Ending the call now."
)


@final
class ReplyRecovery:
    """Speaks the right apology and applies the right transition for *exc*."""

    __slots__ = ("_actor", "_speak")
    _actor: CallActor
    _speak: SpeakFn

    def __new__(cls, actor: CallActor, speak: SpeakFn) -> Self:
        self = super().__new__(cls)
        self._actor = actor
        self._speak = speak
        return self

    async def recover(self, exc: SessionAttachError) -> None:
        """React to *exc*, raised while a turn's reply was in flight.

        Waiting has no direct path back to listening (``CallState`` has no
        such transition), so every recovery travels through speaking, same
        as a real reply would -- the apology (or, for a permanent auth
        failure, the goodbye) IS the reply this turn gets.
        """
        self._actor.apply(ReplyBegins())
        if isinstance(exc, BareAuthMissingError):
            # exc_info=exc, not logger.exception(): correct regardless of
            # whether the caller is inside the except block handling *exc*.
            logger.error(
                "session-attach failed: missing ANTHROPIC_API_KEY", exc_info=exc
            )
            await self._speak(_BARE_AUTH_MISSING)
            self._actor.apply(EndCall())
            return
        logger.error("session-attach failed mid-turn", exc_info=exc)
        await self._speak(_SESSION_ATTACH_FAILED)
        self._actor.apply(ReplyEnds())
