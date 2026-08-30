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
        assert wire["items"] == ["(none)", "eleven_v3", "eleven_flash_v2_5"]
        assert wire["selected"] == 2
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
    @pytest.mark.parametrize("current", [None, "eleven_v3", "eleven_flash_v2_5"])
    def test_the_models_start_at_one_whatever_is_chosen(
        self, current: str | None
    ) -> None:
        """Index 0 is always ``(none)``, so the offset never depends on state."""
        control = ModelControl(
            models=("eleven_v3", "eleven_flash_v2_5"), current=current
        )
        assert control.model_for_index(1) == "eleven_v3"
        assert control.model_for_index(2) == "eleven_flash_v2_5"

    def test_clicking_the_sentinel_itself_is_refused(self) -> None:
        control = ModelControl(models=("eleven_v3", "eleven_flash_v2_5"), current=None)
        with pytest.raises(ValueError, match="out of range"):
            control.model_for_index(0)

    @pytest.mark.parametrize("index", [-1, 3])
    def test_out_of_range_index_raises(self, index: int) -> None:
        control = ModelControl(
            models=("eleven_v3", "eleven_flash_v2_5"), current="eleven_v3"
        )
        with pytest.raises(ValueError, match="out of range"):
            control.model_for_index(index)

    def test_modelless_provider_raises_for_any_index(self) -> None:
        control = ModelControl(models=(), current=None)
        with pytest.raises(ValueError, match="out of range"):
            control.model_for_index(0)
