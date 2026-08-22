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
    def voxd_rejected(cls, detail: str) -> Self:
        """Return the warning shown when voxd answered a request with a refusal.

        Distinct from :meth:`voxd_unavailable`: voxd was reached and said no
        -- an unknown voice, an unreadable reply -- so the next click will not
        quietly fix it and the reason is worth carrying into the scene.
        """
        return cls(f"⚠ voxd rejected the request -- {detail}")

    @classmethod
    def write_failed(cls, field: str) -> Self:
        """Return the warning shown when persisting *field* could not be saved."""
        return cls(f"⚠ couldn't save {field} -- reverted to the last saved value")

    @classmethod
    def write_failed_and_voxd_unavailable(cls, field: str) -> Self:
        """Return the warning when a failed *field* persist AND the resync
        meant to confirm the reverted value both fail -- two unrelated
        subsystems (local disk, voxd), so neither message may be dropped."""
        return cls(f"⚠ couldn't save {field}, and voxd is unreachable too")

    @classmethod
    def write_failed_and_voxd_rejected(cls, field: str, detail: str) -> Self:
        """Return the warning when a failed *field* persist AND the resync
        meant to confirm the reverted value hit a daemon refusal -- two
        unrelated failures (local disk, voxd rejection), neither dropped."""
        return cls(f"⚠ couldn't save {field}, and voxd rejected the resync -- {detail}")
