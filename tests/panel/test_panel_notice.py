"""Tests for :mod:`punt_vox.panel.panel_notice`."""

from __future__ import annotations

from punt_vox.panel.panel_notice import PanelNotice


class TestSilent:
    def test_message_is_empty(self) -> None:
        assert PanelNotice.silent().message == ""

    def test_equal_to_another_silent_instance(self) -> None:
        assert PanelNotice.silent() == PanelNotice.silent()


class TestVoxdUnavailable:
    def test_carries_a_non_empty_message(self) -> None:
        assert PanelNotice.voxd_unavailable().message != ""

    def test_distinct_from_silent(self) -> None:
        assert PanelNotice.voxd_unavailable() != PanelNotice.silent()


class TestWriteFailed:
    def test_message_names_the_field(self) -> None:
        assert "voice" in PanelNotice.write_failed("voice").message

    def test_distinct_from_silent(self) -> None:
        assert PanelNotice.write_failed("notify") != PanelNotice.silent()
