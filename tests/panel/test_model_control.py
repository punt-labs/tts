"""Tests for :mod:`punt_vox.panel.model_control`."""

from __future__ import annotations

import pytest

from punt_vox.panel.model_control import ModelControl
from punt_vox.panel.topics import PanelTopic


class TestToDict:
    def test_wire_shape_is_a_labeled_combo(self) -> None:
        control = ModelControl(
            models=("eleven_v3", "eleven_flash_v2_5"), current="eleven_flash_v2_5"
        )
        wire = control.to_dict()
        assert wire["kind"] == "combo"
        assert wire["id"] == "vox.panel.model"
        assert wire["label"] == "Model"
        assert wire["items"] == ["eleven_v3", "eleven_flash_v2_5"]
        assert wire["selected"] == 1
        assert wire["handlers"] == [
            {"event": "changed", "publish": [PanelTopic.MODEL.value]}
        ]

    def test_none_current_selects_first_index(self) -> None:
        control = ModelControl(models=("eleven_v3",), current=None)
        wire = control.to_dict()
        assert wire["selected"] == 0

    def test_unknown_current_selects_first_index(self) -> None:
        control = ModelControl(models=("eleven_v3",), current="mystery")
        wire = control.to_dict()
        assert wire["selected"] == 0

    def test_modelless_provider_renders_a_sentinel_with_no_handler(self) -> None:
        control = ModelControl(models=(), current=None)
        wire = control.to_dict()
        assert wire["kind"] == "combo"
        assert wire["items"] == ["(no models)"]
        assert wire["selected"] == 0
        # No handler means selection cannot publish a change the daemon
        # would refuse for a modelless provider.
        assert "handlers" not in wire


class TestModelForIndex:
    def test_valid_index_returns_its_model(self) -> None:
        control = ModelControl(models=("eleven_v3", "eleven_flash_v2_5"), current=None)
        assert control.model_for_index(1) == "eleven_flash_v2_5"

    @pytest.mark.parametrize("index", [-1, 2])
    def test_out_of_range_index_raises(self, index: int) -> None:
        control = ModelControl(models=("eleven_v3", "eleven_flash_v2_5"), current=None)
        with pytest.raises(ValueError, match="out of range"):
            control.model_for_index(index)

    def test_modelless_provider_raises_for_any_index(self) -> None:
        control = ModelControl(models=(), current=None)
        with pytest.raises(ValueError, match="out of range"):
            control.model_for_index(0)
