"""The ``BargeIn`` command: the human interrupts, speaking -> listening."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.call_state import CallState

__all__ = ["BargeIn"]


@final
@dataclass(frozen=True, slots=True)
class BargeIn:
    """FR-8: the barge-in detector fired; the agent's audio stops.

    Discharges any pending addendum on the same transition -- see
    ``docs/conversation-mode-call-state.tex`` section 5. Barge-in detection
    itself is out of scope for this slice (Slice 2a territory); this command
    exists so the state machine's full operation set is executable now.
    """

    def apply(self, state: CallState) -> None:
        state.barge_in()
