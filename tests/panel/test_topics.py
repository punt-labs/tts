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


class TestWritesField:
    def test_every_field_writing_topic_answers_yes_and_has_a_field(self) -> None:
        """The guard and the partial table must agree for every member.

        ``writes_field`` exists to be asked before ``field_name``, so a
        member the guard waves through but the table has no entry for would
        raise on exactly the failure path that guard protects.
        """
        for topic in PanelTopic:
            if topic.writes_field:
                assert topic.field_name  # must not raise

    def test_voice_preview_is_the_one_topic_that_commits_nothing(self) -> None:
        assert not PanelTopic.VOICE_PREVIEW.writes_field
        assert [t for t in PanelTopic if not t.writes_field] == [
            PanelTopic.VOICE_PREVIEW
        ]


class TestLabel:
    def test_every_topic_has_a_label(self) -> None:
        """Total where ``field_name`` is partial.

        A failure on any topic can put its name in front of a user, so a
        topic without a label would raise on the path whose whole job is
        explaining a failure.
        """
        for topic in PanelTopic:
            assert topic.label

    def test_no_label_leaks_the_wire_value(self) -> None:
        for topic in PanelTopic:
            assert topic.label != topic.value
            assert "vox." not in topic.label
