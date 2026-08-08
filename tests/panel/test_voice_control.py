"""Tests for :mod:`punt_vox.panel.voice_control`."""

from __future__ import annotations

from typing import cast

import pytest

from punt_vox.panel.topics import PanelTopic
from punt_vox.panel.voice_control import VoiceControl


def _children(wire: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    combo, button = cast("list[dict[str, object]]", wire["children"])
    return combo, button


class TestToDict:
    def test_wire_shape_is_columns_group_of_combo_and_button(self) -> None:
        control = VoiceControl(roster=("aria", "roger"), current="roger")
        wire = control.to_dict()
        assert wire["kind"] == "group"
        assert wire["layout"] == "columns"
        combo, button = _children(wire)
        assert combo["kind"] == "combo"
        assert combo["items"] == ["aria", "roger"]
        assert combo["selected"] == 1
        assert combo["handlers"] == [
            {"event": "changed", "publish": [PanelTopic.VOICE.value]}
        ]
        assert button["kind"] == "button"
        assert button["label"] == "▶"
        assert button["tooltip"] == "Preview"
        assert button["publish"] == {"topic": PanelTopic.VOICE_PREVIEW.value}

    def test_none_current_selects_first_index(self) -> None:
        control = VoiceControl(roster=("aria", "roger"), current=None)
        combo, _ = _children(control.to_dict())
        assert combo["selected"] == 0

    def test_unknown_current_selects_first_index(self) -> None:
        control = VoiceControl(roster=("aria", "roger"), current="nobody")
        combo, _ = _children(control.to_dict())
        assert combo["selected"] == 0


class TestVoiceForIndex:
    def test_valid_index_returns_its_voice(self) -> None:
        control = VoiceControl(roster=("aria", "roger"), current=None)
        assert control.voice_for_index(1) == "roger"

    @pytest.mark.parametrize("index", [-1, 2])
    def test_out_of_range_index_raises(self, index: int) -> None:
        control = VoiceControl(roster=("aria", "roger"), current=None)
        with pytest.raises(ValueError, match="out of range"):
            control.voice_for_index(index)
