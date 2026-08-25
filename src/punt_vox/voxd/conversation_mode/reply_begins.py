"""The ``ReplyBegins`` command: the reply is ready to play, waiting -> speaking."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.call_state import CallState

__all__ = ["ReplyBegins"]


@final
@dataclass(frozen=True, slots=True)
class ReplyBegins:
    """FR-11: the reply's first complete portion is ready; start speaking it."""

    def apply(self, state: CallState) -> None:
        state.reply_begins()
