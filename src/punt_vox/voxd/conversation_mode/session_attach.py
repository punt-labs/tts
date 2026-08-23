"""The seam between a live call and the human's already-running agent session.

:class:`SessionAttach` is the interface :mod:`~punt_vox.voxd.conversation_mode`
calls to forward a transcribed human turn and receive the agent's reply as it
streams in -- FR-4's requirement that a call use the user's already-running
session, not a fresh one, and FR-11's requirement that speech begin on the
reply's first complete portion. Production backs it with the mechanism
recommended in ``docs/conversation-mode-session-attach-adr.md`` (a headless
``claude --resume`` subprocess speaking ``stream-json``), pending operator
ratification (DES-064); tests inject
:class:`~tests.conversation_mode._session_attach_fakes.FakeSessionAttach`. No
method takes a session identifier -- one :class:`SessionAttach` instance is
already bound to one call's session for its lifetime, the same reasoning
``docs/audio-programs.tex`` gives for :class:`~punt_vox.voxd.programs.Program`
carrying no owner, applied here to "no session" instead of "no owner" because
the session *is* the whole point of the object rather than an omitted axis.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Protocol, Self, runtime_checkable

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.reply import ReplyChunk
    from punt_vox.voxd.conversation_mode.turn import TranscribedTurn

__all__ = ["BareAuthMissingError", "SessionAttach", "SessionAttachError"]


class SessionAttachError(RuntimeError):
    """The session could not be reached, or ended the turn abnormally.

    A boundary error (PY-EH-1): raised at the point ``voxd`` learns the
    agent session is unreachable or has stopped answering mid-turn, not
    caught and retried internally -- per the PRD's non-goal of automatic
    mid-call resilience, a call that hits this ends with a clear reason
    (mirroring FR-18's fail-closed call start), it does not silently retry.
    """


class BareAuthMissingError(SessionAttachError):
    """A ``--bare`` invocation has no ``ANTHROPIC_API_KEY`` to authenticate with.

    Raised before the subprocess is even spawned (see
    :class:`~.claude_session_attach.ClaudeSessionAttach`), not after a
    doomed spawn times out -- ``claude --bare`` has no OAuth fallback at
    all, so a missing key is a certain, immediate failure, not a transient
    condition worth retrying into a 120s timeout.
    """

    @classmethod
    def for_missing_key(cls) -> Self:
        """Build the error for a ``--bare`` invocation with no key configured.

        The error describes its own actionable message (PY-CC-5) so the
        caller checking for the key does not also own the wording -- one
        place to update if the guidance ever changes.
        """
        msg = (
            "claude -p --resume --bare requires ANTHROPIC_API_KEY to be "
            "set -- bare mode has no OAuth support (see `claude --help`); "
            "set ANTHROPIC_API_KEY before running `vox call start`"
        )
        return cls(msg)


@runtime_checkable
class SessionAttach(Protocol):
    """One human turn in, one streamed agent reply out, for one call's session."""

    def send_turn(self, turn: TranscribedTurn) -> AsyncIterator[ReplyChunk]:
        """Forward ``turn`` to the agent session and stream its reply back.

        Returns an async iterator, not an awaited value: the caller starts
        consuming chunks (and can start speaking, per FR-11) before the
        agent has finished composing the rest of the reply. Raises
        :class:`SessionAttachError` if the session cannot be reached or ends
        the turn abnormally.
        """
        ...
