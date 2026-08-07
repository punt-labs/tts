"""``PanelNotice`` -- a transient status the panel's scene projection carries.

A control change can fail after the UI already shows it optimistically:
persisting the new value to config can raise, or voxd can be unreachable for a
preview or a background refresh. Either way the scene must say so rather than
silently reverting with no explanation. This is a Null Object (PY-DP-9):
:meth:`silent` is the normal state and renders no status line; the named
failure constructors each carry a one-line, user-facing message.
:class:`~punt_vox.panel.service.VoxPanelService` holds it alongside its
settings snapshot, never as a flag baked into that snapshot, so a recovered
outage clears cleanly back to silent without touching the settings it
describes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self, final

__all__ = ["PanelNotice"]


@final
@dataclass(frozen=True, slots=True)
class PanelNotice:
    """A transient scene status: a warning message, or silent (the Null state)."""

    message: str  # the empty string is the silent Null state -- no status line

    @classmethod
    def silent(cls) -> Self:
        """Return the silent notice -- the normal state, rendering no status line."""
        return cls("")

    @classmethod
    def voxd_unavailable(cls) -> Self:
        """Return the warning shown when voxd could not be reached."""
        return cls("⚠ voxd is unreachable -- showing the last known settings")

    @classmethod
    def write_failed(cls, field: str) -> Self:
        """Return the warning shown when persisting *field* could not be saved."""
        return cls(f"⚠ couldn't save {field} -- reverted to the last saved value")
