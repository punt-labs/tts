"""``ProviderControl`` -- the TTS provider combo, one of the Voice engine trio.

Mirrors :class:`~punt_vox.panel.voice_control.VoiceControl` in shape: a
dataclass that projects a fixed list plus the current selection onto a combo
element ready to render. The provider list is the closed enum
:data:`~punt_vox.server_switches.PROVIDER_NAMES` (§3.2): adding a provider is
a code change, never a runtime discovery, so the panel embeds the same
static list the MCP tool exposes.

A session that has chosen no provider is shown ``(none)`` rather than the
first provider in the enum -- see :class:`~punt_vox.panel.choice_list.ChoiceList`,
which owns both the offered list and the click resolution so the two cannot
disagree about whether that entry is present.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, final

from punt_vox.panel.choice_list import ChoiceList
from punt_vox.panel.topics import PanelTopic

__all__ = ["ProviderControl"]

_COMBO_ID: Final = "vox.panel.provider"
# The enum is never empty, so this label is unreachable in practice; the
# choice list still requires one rather than inventing wording of its own.
_EMPTY_LABEL: Final = "(no providers)"


@final
@dataclass(frozen=True, slots=True)
class ProviderControl:
    """Project the provider enum and current choice onto a labeled combo."""

    providers: tuple[str, ...]
    current: str | None

    def to_dict(self) -> dict[str, object]:
        """Return the combo's wire dict, selected at ``current``'s entry."""
        choices = self._choices()
        return {
            "kind": "combo",
            "id": _COMBO_ID,
            "label": "Provider",
            "items": choices.wire_items(),
            "selected": choices.selected_index(),
            "handlers": [{"event": "changed", "publish": [PanelTopic.PROVIDER.value]}],
        }

    def provider_for_index(self, index: int) -> str:
        """Return the provider name a clicked ``index`` selects, or raise if invalid."""
        return self._choices().name_for_index(index, noun="providers")

    def _choices(self) -> ChoiceList:
        return ChoiceList(
            items=self.providers, current=self.current, empty_label=_EMPTY_LABEL
        )
