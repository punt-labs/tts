"""``PanelScene`` -- the pure projection of vox's current settings onto a scene.

Three regions top to bottom: the Notifications radio, the Mic Mode radio, and
the Voice combo + preview button, separated by rules -- the layout locked in
``docs/vox-control-panel-ui.md``. ``frame.flags.auto_resize`` is set because
lux's default frame is a fixed 75% of the window, which leaves this small,
flat control set floating in empty space; auto-resize recomputes the frame to
fit it every push.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import final

from punt_lux import RenderRequest, SeparatorElement
from punt_lux.operations.models import FrameFlags, FrameSpec

from punt_vox.panel.radio_control import MIC_MODE_SPEC, NOTIFY_SPEC
from punt_vox.panel.voice_control import VoiceControl

__all__ = ["PanelScene"]

_SCENE_ID = "vox.panel"
_TITLE = "Vox"


@final
@dataclass(frozen=True, slots=True)
class PanelScene:
    """Project the panel's current settings onto the ``vox.panel`` scene."""

    notify: str
    speak: str
    voice: str | None
    roster: tuple[str, ...]

    def render_request(self) -> RenderRequest:
        """Return the whole panel scene: notify radio, mic-mode radio, voice row."""
        elements = [
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
            frame=FrameSpec(flags=FrameFlags(auto_resize=True)),
        )
