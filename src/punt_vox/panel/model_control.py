"""``ModelControl`` -- the TTS model combo, one of the Voice engine trio.

Mirrors :class:`~punt_vox.panel.voice_control.VoiceControl` in shape: a
dataclass that projects a list plus the current selection onto a combo
element ready to render. The model list is provider-scoped (§3.1) --
:data:`~punt_vox.models.MODEL_TABLE`.available(provider). A modelless
provider (Polly, ``say``, ``espeak``) renders the combo with a single
``(no models)`` sentinel item and no handler, so the widget shows the
missing selection without publishing a change the daemon would refuse.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, final

from punt_vox.panel.topics import PanelTopic

__all__ = ["ModelControl"]

_COMBO_ID: Final = "vox.panel.model"
_NONE_ITEM: Final = "(no models)"


@final
@dataclass(frozen=True, slots=True)
class ModelControl:
    """Project the provider's model list and current choice onto a labeled combo."""

    models: tuple[str, ...]
    current: str | None

    def to_dict(self) -> dict[str, object]:
        """Return the combo's wire dict; a modelless provider renders inert."""
        if not self.models:
            return {
                "kind": "combo",
                "id": _COMBO_ID,
                "label": "Model",
                "items": [_NONE_ITEM],
                "selected": 0,
            }
        return {
            "kind": "combo",
            "id": _COMBO_ID,
            "label": "Model",
            "items": list(self.models),
            "selected": self._selected_index(),
            "handlers": [{"event": "changed", "publish": [PanelTopic.MODEL.value]}],
        }

    def model_for_index(self, index: int) -> str:
        """Return the model name a clicked ``index`` selects, or raise if invalid."""
        if not 0 <= index < len(self.models):
            count = len(self.models)
            msg = f"model combo: index {index} out of range for {count} models"
            raise ValueError(msg)
        return self.models[index]

    def _selected_index(self) -> int:
        """Return ``current``'s index into the model list, or 0 when absent/unknown."""
        if self.current is None or self.current not in self.models:
            return 0
        return self.models.index(self.current)
