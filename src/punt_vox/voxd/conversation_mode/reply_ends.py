"""The ``ReplyEnds`` command: the reply finished cleanly, speaking -> listening."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.call_state import CallState

__all__ = ["ReplyEnds"]


@final
@dataclass(frozen=True, slots=True)
class ReplyEnds:
    """The reply finished without interruption; signal ready-to-listen (NFR-6)."""

    def apply(self, state: CallState) -> None:
        state.reply_ends()
