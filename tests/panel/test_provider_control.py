"""Tests for :mod:`punt_vox.panel.provider_control`."""

from __future__ import annotations

import pytest

from punt_vox.panel.provider_control import ProviderControl
from punt_vox.panel.topics import PanelTopic


class TestToDict:
    def test_wire_shape_is_a_labeled_combo(self) -> None:
        control = ProviderControl(providers=("elevenlabs", "openai"), current="openai")
        wire = control.to_dict()
        assert wire["kind"] == "combo"
        assert wire["id"] == "vox.panel.provider"
        assert wire["label"] == "Provider"
        assert wire["items"] == ["elevenlabs", "openai"]
        assert wire["selected"] == 1
        assert wire["handlers"] == [
            {"event": "changed", "publish": [PanelTopic.PROVIDER.value]}
        ]

    def test_none_current_selects_first_index(self) -> None:
        control = ProviderControl(providers=("elevenlabs", "openai"), current=None)
        wire = control.to_dict()
        assert wire["selected"] == 0

    def test_unknown_current_selects_first_index(self) -> None:
        control = ProviderControl(providers=("elevenlabs", "openai"), current="mystery")
        wire = control.to_dict()
        assert wire["selected"] == 0


class TestProviderForIndex:
    def test_valid_index_returns_its_provider(self) -> None:
        control = ProviderControl(providers=("elevenlabs", "openai"), current=None)
        assert control.provider_for_index(1) == "openai"

    @pytest.mark.parametrize("index", [-1, 2])
    def test_out_of_range_index_raises(self, index: int) -> None:
        control = ProviderControl(providers=("elevenlabs", "openai"), current=None)
        with pytest.raises(ValueError, match="out of range"):
            control.provider_for_index(index)
