"""The ``StartCall`` command: begin a call, idle -> listening."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.call_state import CallState

__all__ = ["StartCall"]


@final
@dataclass(frozen=True, slots=True)
class StartCall:
    """Enqueued once, at call start, to move ``CallState`` into ``listening``."""

    def apply(self, state: CallState) -> None:
        state.start_call()
