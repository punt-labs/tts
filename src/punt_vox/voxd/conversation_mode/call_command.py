"""The ``CallCommand`` interface -- one serialized state-machine transition.

Mirrors ``voxd/programs/control_signal.py``'s shape: each transition the Z
specification names is a typed command that knows how to apply itself
(Command pattern, PY-DP-11), rather than a bare tag a dispatcher switches on.
:class:`~.call_actor.CallActor` calls :meth:`CallCommand.apply` on whatever
command it dequeues next; it never inspects a command's type.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.call_state import CallState

__all__ = ["CallCommand"]


@runtime_checkable
class CallCommand(Protocol):
    """A single command applied to :class:`CallState` by the sole dispatch consumer."""

    def apply(self, state: CallState) -> None:
        """Apply this command's transition to *state*."""
        ...
