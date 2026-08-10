"""``ProviderControl`` -- the TTS provider combo, one of the Voice engine trio.

Mirrors :class:`~punt_vox.panel.voice_control.VoiceControl` in shape: a
dataclass that projects a fixed list plus the current selection onto a combo
element ready to render. The provider list is the closed enum
:data:`~punt_vox.server_switches.PROVIDER_NAMES` (§3.2): adding a provider is
a code change, never a runtime discovery, so the panel embeds the same
static list the MCP tool exposes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, final

from punt_vox.panel.topics import PanelTopic

__all__ = ["ProviderControl"]

_COMBO_ID: Final = "vox.panel.provider"


@final
@dataclass(frozen=True, slots=True)
class ProviderControl:
    """Project the provider enum and current choice onto a labeled combo."""

    providers: tuple[str, ...]
    current: str | None

    def to_dict(self) -> dict[str, object]:
        """Return the combo's wire dict, selected at ``current``'s index."""
        return {
            "kind": "combo",
            "id": _COMBO_ID,
            "label": "Provider",
            "items": list(self.providers),
            "selected": self._selected_index(),
            "handlers": [{"event": "changed", "publish": [PanelTopic.PROVIDER.value]}],
        }

    def provider_for_index(self, index: int) -> str:
        """Return the provider name a clicked ``index`` selects, or raise if invalid."""
        if not 0 <= index < len(self.providers):
            count = len(self.providers)
            msg = f"provider combo: index {index} out of range for {count} providers"
            raise ValueError(msg)
        return self.providers[index]

    def _selected_index(self) -> int:
        """Return ``current``'s index into the enum, or 0 when absent/unknown."""
        if self.current is None or self.current not in self.providers:
            return 0
        return self.providers.index(self.current)
