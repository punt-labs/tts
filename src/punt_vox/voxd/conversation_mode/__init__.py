"""Conversation Mode: a live voice call between one human and one agent session.

This package holds the pure domain seams Conversation Mode needs -- currently
just :class:`~punt_vox.voxd.conversation_mode.session_attach.SessionAttach`,
the interface between a live call and the human's already-running Claude Code
session (``docs/conversation-mode-session-attach-adr.md``). No I/O and no
daemon wiring live here yet; Slice 1b adds the real implementation once the
operator ratifies a session-attach mechanism.
"""

from __future__ import annotations

from punt_vox.voxd.conversation_mode.reply import ReplyChunk
from punt_vox.voxd.conversation_mode.session_attach import (
    SessionAttach,
    SessionAttachError,
)
from punt_vox.voxd.conversation_mode.turn import TranscribedTurn

__all__ = [
    "ReplyChunk",
    "SessionAttach",
    "SessionAttachError",
    "TranscribedTurn",
]
