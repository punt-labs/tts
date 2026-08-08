"""``RadioSpec``/``RadioControl`` -- a labeled radio group over a fixed code map.

Notifications and Mic Mode are both "pick one of a few labeled choices, and
each choice writes one config code" -- the same shape with different labels
and codes. ``RadioSpec`` is that fixed shape; ``RadioControl`` is a spec
projected onto one current value, ready to render.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, final

from punt_vox.panel.topics import PanelTopic

__all__ = ["MIC_MODE_SPEC", "NOTIFY_SPEC", "RadioControl", "RadioSpec"]


@final
@dataclass(frozen=True, slots=True)
class RadioSpec:
    """The fixed shape of one radio control: its identity, labels, and codes.

    ``items`` and ``codes`` are parallel tuples -- ``items[i]`` is the label a
    user reads, ``codes[i]`` is the value vox's config store holds for that
    choice.
    """

    element_id: str
    label: str
    items: tuple[str, ...]
    codes: tuple[str, ...]
    topic: PanelTopic

    def control_for(self, current: str) -> RadioControl:
        """Return the control projecting ``current`` onto this spec's items."""
        return RadioControl(spec=self, current=current)

    def code_for_index(self, index: int) -> str:
        """Return the code a clicked ``index`` selects, or raise if out of range."""
        if not 0 <= index < len(self.codes):
            msg = f"{self.element_id}: index {index} out of range for {self.codes!r}"
            raise ValueError(msg)
        return self.codes[index]


NOTIFY_SPEC: Final = RadioSpec(
    element_id="vox.panel.notify",
    label="Notifications",
    items=("Off", "Normal", "Continuous"),
    codes=("n", "y", "c"),
    topic=PanelTopic.NOTIFY,
)
MIC_MODE_SPEC: Final = RadioSpec(
    element_id="vox.panel.mic_mode",
    label="Mic Mode",
    items=("Chimes only", "Voice"),
    codes=("n", "y"),
    topic=PanelTopic.MIC_MODE,
)


@final
@dataclass(frozen=True, slots=True)
class RadioControl:
    """Project a current code onto its spec's radio group, for the scene."""

    spec: RadioSpec
    current: str

    def to_dict(self) -> dict[str, object]:
        """Return the radio's wire dict, selected at the index for ``current``."""
        return {
            "kind": "radio",
            "id": self.spec.element_id,
            "label": self.spec.label,
            "items": list(self.spec.items),
            "selected": self._selected_index(),
            "handlers": [{"event": "changed", "publish": [self.spec.topic.value]}],
        }

    def _selected_index(self) -> int:
        """Return ``current``'s index into the spec's codes, or 0 if unknown."""
        codes = self.spec.codes
        return codes.index(self.current) if self.current in codes else 0
