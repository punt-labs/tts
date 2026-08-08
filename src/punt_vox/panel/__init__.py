"""The Vox control panel: a session-scoped Lux applet for notify/mic/voice settings.

Menu-launched, not daemon-hosted: it reads vox's current settings once per
click, and applies a change the moment a control fires -- see
``docs/vox-control-panel-ui.md`` for the full design.
"""

from __future__ import annotations

from punt_vox.panel.applet import VoxPanelApplet

__all__ = ["VoxPanelApplet"]
