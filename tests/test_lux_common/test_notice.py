"""Tests for :mod:`punt_vox.lux_common.notice`."""

from __future__ import annotations

from punt_vox.lux_common.notice import LuxNotice


class TestSilent:
    def test_message_is_empty(self) -> None:
        assert LuxNotice.silent().message == ""

    def test_is_not_present(self) -> None:
        assert not LuxNotice.silent().is_present

    def test_equal_to_another_silent_instance(self) -> None:
        assert LuxNotice.silent() == LuxNotice.silent()


class TestWarning:
    def test_carries_the_message(self) -> None:
        assert LuxNotice.warning("boom").message == "boom"

    def test_is_present(self) -> None:
        assert LuxNotice.warning("boom").is_present

    def test_distinct_from_silent(self) -> None:
        assert LuxNotice.warning("boom") != LuxNotice.silent()

    def test_equal_to_another_warning_with_the_same_message(self) -> None:
        assert LuxNotice.warning("boom") == LuxNotice.warning("boom")
