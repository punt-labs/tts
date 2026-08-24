"""The ``TurnDetected`` command: a closed run moves listening -> waiting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.call_state import CallState

__all__ = ["TurnDetected"]


@final
@dataclass(frozen=True, slots=True)
class TurnDetected:
    """FR-5: the turn detector fired end-of-turn; listening -> waiting.

    Carries no payload, the same shape as its zero-field siblings
    (:class:`~.end_call.EndCall`, :class:`~.timeout_call.TimeoutCall`) --
    the transcribed turn it announces is forwarded by the caller
    (:class:`~.call_session.CallSession`, which already holds it in a local
    variable) directly to session-attach, never read back off this command.
    """

    def apply(self, state: CallState) -> None:
        state.turn_detected()
