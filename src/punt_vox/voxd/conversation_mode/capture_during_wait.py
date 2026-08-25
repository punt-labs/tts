"""The ``CaptureDuringWait`` command: a run closes while already waiting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.call_state import CallState

__all__ = ["CaptureDuringWait"]


@final
@dataclass(frozen=True, slots=True)
class CaptureDuringWait:
    """The turn detector fired again mid-wait; records a pending addendum.

    FR-4 rules out dispatching a second, concurrent turn while the first is
    still in flight, so this speech is held (:attr:`CallState.has_pending_addendum`)
    rather than sent or discarded -- see ``docs/conversation-mode-call-state.tex``
    section 5.
    """

    def apply(self, state: CallState) -> None:
        state.capture_during_wait()
