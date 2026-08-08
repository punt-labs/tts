"""``PanelScene`` -- the pure projection of vox's current settings onto a scene.

Four regions top to bottom: a one-line status slot, the Notifications radio,
the Mic Mode radio, and the Voice combo + preview button, separated by rules --
the layout locked in ``docs/vox-control-panel-ui.md``. The status slot renders
empty unless :class:`~punt_vox.panel.panel_notice.PanelNotice` carries a
warning, so the scene's shape never changes between the silent and the
warning case.

The frame uses a fixed ``size`` rather than ``frame.flags.auto_resize``: this
panel's section labels render as ImGui's ``separator_text`` widget, which by
design always spans the full available width -- the same as a plain
separator. That intentional full-width element feeds back into
``auto_resize``'s size computation, so the frame can never converge narrower
than the display's default fill no matter how compact the actual controls
are. A frame sized to the real content is the only fix for a static panel
like this one; ``auto_resize`` remains the right tool for content whose size
genuinely varies. ``frame_size`` only takes effect on a frame's first
creation -- a later push with a different size is silently ignored -- so this
size must be correct from the panel's very first render.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import final

from punt_lux import RenderRequest, SeparatorElement, TextElement
from punt_lux.operations.models import FrameSpec

from punt_vox.panel.panel_notice import PanelNotice
from punt_vox.panel.radio_control import MIC_MODE_SPEC, NOTIFY_SPEC
from punt_vox.panel.voice_control import VoiceControl

__all__ = ["PanelScene"]

_SCENE_ID = "vox.panel"
_TITLE = "Vox"
_STATUS_ID = "vox.panel.status"
# The widest row (the Voice combo plus its inline preview button) measured
# ~330-350px wide by ~220-260px tall against the locked mockup; splitting the
# difference gives headroom on both axes without inflating the frame.
_FRAME_SIZE = (340, 240)


@final
@dataclass(frozen=True, slots=True)
class PanelScene:
    """Project the panel's current settings and notice onto the ``vox.panel`` scene."""

    notify: str
    speak: str
    voice: str | None
    roster: tuple[str, ...]
    notice: PanelNotice = field(default_factory=PanelNotice.silent)

    def render_request(self) -> RenderRequest:
        """Return the whole panel scene: status line, both radios, voice row."""
        elements = [
            TextElement(id=_STATUS_ID, content=self.notice.message).to_dict(),
            NOTIFY_SPEC.control_for(self.notify).to_dict(),
            SeparatorElement(id="vox.panel.sep1").to_dict(),
            MIC_MODE_SPEC.control_for(self.speak).to_dict(),
            SeparatorElement(id="vox.panel.sep2").to_dict(),
            VoiceControl(roster=self.roster, current=self.voice).to_dict(),
        ]
        return RenderRequest(
            scene_id=_SCENE_ID,
            elements=elements,
            title=_TITLE,
            frame=FrameSpec(size=_FRAME_SIZE),
        )
