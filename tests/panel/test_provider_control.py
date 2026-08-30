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
        assert wire["items"] == ["(none)", "elevenlabs", "openai"]
        assert wire["selected"] == 2
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
    @pytest.mark.parametrize("current", [None, "elevenlabs", "openai"])
    def test_the_providers_start_at_one_whatever_is_chosen(
        self, current: str | None
    ) -> None:
        """Index 0 is always ``(none)``, so the offset never depends on state.

        A click carries an index picked from the list as rendered and is
        resolved against state read later. An offset that came and went
        with the current value would shift the whole mapping under a click
        already in flight, committing the wrong provider silently.
        """
        control = ProviderControl(providers=("elevenlabs", "openai"), current=current)
        assert control.provider_for_index(1) == "elevenlabs"
        assert control.provider_for_index(2) == "openai"

    def test_clicking_the_sentinel_itself_is_refused(self) -> None:
        control = ProviderControl(providers=("elevenlabs", "openai"), current=None)
        with pytest.raises(ValueError, match="out of range"):
            control.provider_for_index(0)

    @pytest.mark.parametrize("index", [-1, 3])
    def test_out_of_range_index_raises(self, index: int) -> None:
        control = ProviderControl(providers=("elevenlabs", "openai"), current="openai")
        with pytest.raises(ValueError, match="out of range"):
            control.provider_for_index(index)
