"""The one-method listener protocol for voxd state-change notifications."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["ChangeListener"]


@runtime_checkable
class ChangeListener(Protocol):
    """A subscriber notified after every applied playback command or catalog edit.

    The single method is deliberately side-effect-only and must not block: it is
    called from the control-channel single-writer, so an implementation reads the
    daemon's fresh status and hands the derived scene to its own async task rather
    than doing I/O inline (PY-DP-11).
    """

    def notify_changed(self) -> None:
        """React to a state change -- re-read status and re-project, never block."""
        ...
