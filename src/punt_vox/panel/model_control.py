"""``ModelControl`` -- the TTS model combo, one of the Voice engine trio.

Mirrors :class:`~punt_vox.panel.voice_control.VoiceControl` in shape: a
dataclass that projects a list plus the current selection onto a combo
element ready to render. The model list is provider-scoped (§3.1) --
:data:`~punt_vox.models.MODEL_TABLE`.available(provider), which answers the
empty tuple both for a modelless provider (Polly, ``say``, ``espeak``) and
for a session that has chosen no provider at all.

Two absent states, told apart: nothing to choose from renders the inert
``(no models)`` sentinel with no handler, so no click can publish a change the
daemon would refuse; a real list with nothing chosen renders ``(none)`` ahead
of it and stays live. :class:`~punt_vox.panel.choice_list.ChoiceList` owns
both, and the resolver reads the same object, so the offered list and the
click can never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, final

from punt_vox.panel.choice_list import ChoiceList
from punt_vox.panel.topics import PanelTopic

__all__ = ["ModelControl"]

_COMBO_ID: Final = "vox.panel.model"
_EMPTY_LABEL: Final = "(no models)"


@final
@dataclass(frozen=True, slots=True)
class ModelControl:
    """Project the provider's model list and current choice onto a labeled combo."""

    models: tuple[str, ...]
    current: str | None

    def to_dict(self) -> dict[str, object]:
        """Return the combo's wire dict; a modelless provider renders inert."""
        choices = self._choices()
        combo: dict[str, object] = {
            "kind": "combo",
            "id": _COMBO_ID,
            "label": "Model",
            "items": choices.wire_items(),
            "selected": choices.selected_index(),
        }
        if not choices.is_empty:
            combo["handlers"] = [
                {"event": "changed", "publish": [PanelTopic.MODEL.value]}
            ]
        return combo

    def model_for_index(self, index: int) -> str:
        """Return the model name a clicked ``index`` selects, or raise if invalid."""
        return self._choices().name_for_index(index, noun="models")

    def _choices(self) -> ChoiceList:
        return ChoiceList(
            items=self.models, current=self.current, empty_label=_EMPTY_LABEL
        )
