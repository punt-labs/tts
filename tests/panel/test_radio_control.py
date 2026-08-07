"""Tests for :mod:`punt_vox.panel.radio_control`."""

from __future__ import annotations

import pytest

from punt_vox.panel.radio_control import MIC_MODE_SPEC, NOTIFY_SPEC
from punt_vox.panel.topics import PanelTopic


class TestRadioControlToDict:
    def test_selected_index_matches_current_code(self) -> None:
        control = NOTIFY_SPEC.control_for("y")
        assert control.to_dict()["selected"] == 1

    def test_unknown_current_code_defaults_to_zero(self) -> None:
        control = NOTIFY_SPEC.control_for("bogus")
        assert control.to_dict()["selected"] == 0

    def test_wire_shape_is_a_radio_with_publish_handler(self) -> None:
        wire = NOTIFY_SPEC.control_for("n").to_dict()
        assert wire["kind"] == "radio"
        assert wire["id"] == "vox.panel.notify"
        assert wire["items"] == ["Off", "Normal", "Continuous"]
        assert wire["handlers"] == [
            {"event": "changed", "publish": [PanelTopic.NOTIFY.value]}
        ]

    def test_mic_mode_has_two_items(self) -> None:
        wire = MIC_MODE_SPEC.control_for("n").to_dict()
        assert wire["items"] == ["Chimes only", "Voice"]


class TestCodeForIndex:
    @pytest.mark.parametrize(("index", "code"), [(0, "n"), (1, "y"), (2, "c")])
    def test_valid_index_returns_its_code(self, index: int, code: str) -> None:
        assert NOTIFY_SPEC.code_for_index(index) == code

    @pytest.mark.parametrize("index", [-1, 3, 99])
    def test_out_of_range_index_raises(self, index: int) -> None:
        with pytest.raises(ValueError, match="out of range"):
            NOTIFY_SPEC.code_for_index(index)
