"""``VoiceControl`` -- the voice combo and its inline preview button.

Per the confirmed layout (``docs/vox-control-panel-ui.md``): the combo and
the glyph-only preview button sit side by side in one ``columns`` group, so
picking a voice is try-before-you-set rather than blind.

The two absent states differ. An empty roster means nothing to preview and
nothing to pick, so both halves go inert. A roster with no voice chosen keeps
both live and shows ``(none)`` as the selection --
:class:`~punt_vox.panel.choice_list.ChoiceList` owns that entry for all three
of the panel's combos, and resolves clicks against the same object it built
the list from.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, final

from punt_vox.panel.choice_list import ChoiceList
from punt_vox.panel.topics import PanelTopic

__all__ = ["VoiceControl"]

_ROW_ID: Final = "vox.panel.voice.row"
_COMBO_ID: Final = "vox.panel.voice"
_PREVIEW_ID: Final = "vox.panel.voice.preview"
_PREVIEW_GLYPH: Final = "▶"
_EMPTY_LABEL: Final = "(no voices)"


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
        return self._choices().name_for_index(index, noun="voice")

    def _choices(self) -> ChoiceList:
        return ChoiceList(
            items=self.roster, current=self.current, empty_label=_EMPTY_LABEL
        )

    def _combo(self) -> dict[str, object]:
        """Return the voice combo; an empty roster renders it inert.

        A control with nothing to pick does not publish, so it cannot send
        back an index :meth:`voice_for_index` would refuse.
        """
        choices = self._choices()
        combo: dict[str, object] = {
            "kind": "combo",
            "id": _COMBO_ID,
            "label": "Voice",
            "items": choices.wire_items(),
            "selected": choices.selected_index(),
        }
        if not choices.is_empty:
            combo["handlers"] = [
                {"event": "changed", "publish": [PanelTopic.VOICE.value]}
            ]
        return combo

    def _preview_button(self) -> dict[str, object]:
        """Return the preview button; it goes inert alongside an empty roster.

        The button previews the *held* voice, and a session with no roster
        holds none -- the service's preview would log a line and return
        having played nothing. A button that publishes nothing reads as
        unavailable instead of broken.
        """
        button: dict[str, object] = {
            "kind": "button",
            "id": _PREVIEW_ID,
            "label": _PREVIEW_GLYPH,
            "tooltip": "Preview",
        }
        if self.roster:
            button["publish"] = {"topic": PanelTopic.VOICE_PREVIEW.value}
        return button
