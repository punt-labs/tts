"""``VoiceControl`` -- the voice combo and its inline preview button.

Per the confirmed layout (``docs/vox-control-panel-ui.md``): the combo and
the glyph-only preview button sit side by side in one ``columns`` group, so
picking a voice is try-before-you-set rather than blind.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, final

from punt_vox.panel.topics import PanelTopic

__all__ = ["VoiceControl"]

_ROW_ID: Final = "vox.panel.voice.row"
_COMBO_ID: Final = "vox.panel.voice"
_PREVIEW_ID: Final = "vox.panel.voice.preview"
_PREVIEW_GLYPH: Final = "▶"


@final
@dataclass(frozen=True, slots=True)
class VoiceControl:
    """Project the voice roster and current choice onto a combo + preview button."""

    roster: tuple[str, ...]
    current: str | None

    def to_dict(self) -> dict[str, object]:
        """Return the combo and its preview button as one ``columns`` group."""
        return {
            "kind": "group",
            "id": _ROW_ID,
            "layout": "columns",
            "children": [self._combo(), self._preview_button()],
        }

    def voice_for_index(self, index: int) -> str:
        """Return the roster name a clicked ``index`` selects, or raise if invalid."""
        if not 0 <= index < len(self.roster):
            count = len(self.roster)
            msg = f"voice combo: index {index} out of range for {count} voices"
            raise ValueError(msg)
        return self.roster[index]

    def _combo(self) -> dict[str, object]:
        return {
            "kind": "combo",
            "id": _COMBO_ID,
            "label": "Voice",
            "items": list(self.roster),
            "selected": self._selected_index(),
            "handlers": [{"event": "changed", "publish": [PanelTopic.VOICE.value]}],
        }

    def _preview_button(self) -> dict[str, object]:
        return {
            "kind": "button",
            "id": _PREVIEW_ID,
            "label": _PREVIEW_GLYPH,
            "tooltip": "Preview",
            "publish": {"topic": PanelTopic.VOICE_PREVIEW.value},
        }

    def _selected_index(self) -> int:
        """Return ``current``'s index into the roster, or 0 when absent/unknown."""
        if self.current is None or self.current not in self.roster:
            return 0
        return self.roster.index(self.current)
