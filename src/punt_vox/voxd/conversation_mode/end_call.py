"""The ``EndCall`` command: an explicit hangup, any active mode -> idle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.call_state import CallState

__all__ = ["EndCall"]


@final
@dataclass(frozen=True, slots=True)
class EndCall:
    """FR-2's explicit hangup: the human said goodbye or asked to hang up."""

    def apply(self, state: CallState) -> None:
        state.end_call()
