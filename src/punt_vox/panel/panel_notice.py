"""``PanelNotice`` -- a transient status the panel's scene projection carries.

A control change can fail after the UI already shows it optimistically:
persisting the new value to config can raise, or voxd can be unreachable for a
preview or a background refresh. Either way the scene must say so rather than
silently reverting with no explanation. The base :class:`LuxNotice` owns the
Null-Object shape (PY-DP-9); this subclass adds the panel-specific failure
constructors that phrase every warning in one place. The
:class:`~punt_vox.panel.service.VoxPanelService` holds a notice alongside its
settings snapshot, never as a flag baked into that snapshot, so a recovered
outage clears cleanly back to silent without touching the settings it
describes.
"""

from __future__ import annotations

from typing import Self, final

from punt_vox.lux_common.notice import LuxNotice

__all__ = ["PanelNotice"]


@final
class PanelNotice(LuxNotice):
    """The panel's :class:`LuxNotice` -- silent, or one of the named warnings."""

    __slots__ = ()

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
    def control_rejected(cls, control: str) -> Self:
        """Return the warning shown when a control change could not be applied.

        *control* is a human name (``"provider"``, ``"voice preview"``), not
        a wire topic. Deliberately vague about the cause: this covers a
        malformed payload and an out-of-range index alike, neither of which
        the caller can act on. What the caller *can* see is the setting
        snapping back, and this line exists so that revert is not the
        panel's only explanation.
        """
        return cls(f"⚠ that {control} change could not be applied -- reverted")

    @classmethod
    def no_voice_selected(cls) -> Self:
        """Return the warning shown when a preview has no voice to play."""
        return cls("⚠ pick a voice first -- there is nothing to preview yet")

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
