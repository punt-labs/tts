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
        assert combo["items"] == ["(none)", "aria", "roger"]
        assert combo["selected"] == 2
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

    def test_an_empty_roster_renders_both_halves_inert(self) -> None:
        """Nothing to pick and nothing to preview means nothing to click.

        A session whose provider offers no roster used to get a combo with
        an empty item list carrying a live ``changed`` handler, beside a ▶
        button that published a preview of a voice the panel did not hold.
        Both looked functional; neither did anything a user could see.
        """
        control = VoiceControl(roster=(), current=None)
        combo, button = _children(control.to_dict())
        assert combo["items"] == ["(no voices)"]
        assert combo["selected"] == 0
        assert "handlers" not in combo
        assert button["label"] == "▶"
        assert "publish" not in button


class TestVoiceForIndex:
    @pytest.mark.parametrize("current", [None, "aria", "roger"])
    def test_the_roster_starts_at_one_whatever_is_chosen(
        self, current: str | None
    ) -> None:
        """Index 0 is always ``(none)``, so the offset never depends on state."""
        control = VoiceControl(roster=("aria", "roger"), current=current)
        assert control.voice_for_index(1) == "aria"
        assert control.voice_for_index(2) == "roger"

    def test_clicking_the_sentinel_itself_is_refused(self) -> None:
        control = VoiceControl(roster=("aria", "roger"), current=None)
        with pytest.raises(ValueError, match="out of range"):
            control.voice_for_index(0)

    @pytest.mark.parametrize("index", [-1, 3])
    def test_out_of_range_index_raises(self, index: int) -> None:
        control = VoiceControl(roster=("aria", "roger"), current="aria")
        with pytest.raises(ValueError, match="out of range"):
            control.voice_for_index(index)
