"""The ``TurnDetected`` command: a closed run moves listening -> waiting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.call_state import CallState
    from punt_vox.voxd.conversation_mode.turn import TranscribedTurn

__all__ = ["TurnDetected"]


@final
@dataclass(frozen=True, slots=True)
class TurnDetected:
    """FR-5: the turn detector fired end-of-turn; ``turn`` is what to forward.

    Carries the already-transcribed turn so the orchestrator can read it back
    off the command after :meth:`apply` runs, rather than needing a second
    channel to learn what was just detected.
    """

    turn: TranscribedTurn

    def apply(self, state: CallState) -> None:
        state.turn_detected()
