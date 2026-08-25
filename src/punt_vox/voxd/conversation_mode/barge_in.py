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
    ``docs/conversation-mode-call-state.tex`` section 5. A separate barge-in
    *detector* task drives this command; it does not yet exist, so this
    command's own transition is exercised directly rather than through a
    live detector until one is wired in.
    """

    def apply(self, state: CallState) -> None:
        state.barge_in()
