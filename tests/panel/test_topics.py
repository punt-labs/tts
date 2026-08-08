"""Tests for :mod:`punt_vox.panel.topics`."""

from __future__ import annotations

import pytest

from punt_vox.panel.topics import PanelTopic


class TestFieldName:
    @pytest.mark.parametrize(
        ("topic", "field"),
        [
            (PanelTopic.NOTIFY, "notify"),
            (PanelTopic.MIC_MODE, "speak"),
            (PanelTopic.VOICE, "voice"),
        ],
    )
    def test_maps_wire_topic_to_config_field(
        self, topic: PanelTopic, field: str
    ) -> None:
        assert topic.field_name == field

    def test_field_name_never_equals_the_wire_value(self) -> None:
        for topic in (PanelTopic.NOTIFY, PanelTopic.MIC_MODE, PanelTopic.VOICE):
            assert topic.field_name != topic.value

    def test_voice_preview_has_no_field_to_write(self) -> None:
        with pytest.raises(KeyError):
            _ = PanelTopic.VOICE_PREVIEW.field_name
