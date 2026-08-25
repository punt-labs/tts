"""The ``TimeoutCall`` command: the bounded-inactivity timeout, listening -> idle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.call_state import CallState

__all__ = ["TimeoutCall"]


@final
@dataclass(frozen=True, slots=True)
class TimeoutCall:
    """FR-2's automatic end: nothing was heard for the configured window."""

    def apply(self, state: CallState) -> None:
        state.timeout_call()
