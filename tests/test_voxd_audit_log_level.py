"""Tests for punt_vox.voxd.audit_log_level -- the INFO audit-floor clamp.

The daemon's log level must never rise above INFO, or a token-holding client
could blind the audit trail (Synthesize/Record/Play INFO, rejected-op WARNING,
operation-failed ERROR). These pin the clamp: ``debug``/``info`` pass through,
anything stricter is capped at ``info``, and an unknown name is rejected.
"""

from __future__ import annotations

import logging

import pytest

from punt_vox.voxd.audit_log_level import AuditFloorLevel


class TestPassesThrough:
    """A level at or below the INFO floor is honored unchanged."""

    def test_debug_is_allowed(self) -> None:
        level = AuditFloorLevel.from_name("debug")
        assert level.numeric == logging.DEBUG
        assert level.name == "debug"

    def test_info_is_allowed(self) -> None:
        level = AuditFloorLevel.from_name("info")
        assert level.numeric == logging.INFO
        assert level.name == "info"

    def test_name_is_case_insensitive_and_stripped(self) -> None:
        assert AuditFloorLevel.from_name("  DEBUG ").name == "debug"


class TestClampsToFloor:
    """A stricter-than-INFO request is clamped DOWN to info, never honored."""

    @pytest.mark.parametrize("requested", ["warning", "error", "critical"])
    def test_sub_info_request_is_clamped_to_info(self, requested: str) -> None:
        """`warning`/`error`/`critical` would drop the INFO trail -- clamp to info."""
        level = AuditFloorLevel.from_name(requested)
        assert level.numeric == logging.INFO
        assert level.name == "info"

    def test_constructor_clamps_a_raw_stricter_numeric(self) -> None:
        """Even a raw numeric above INFO is capped in the constructor."""
        assert AuditFloorLevel(logging.WARNING).numeric == logging.INFO

    def test_constructor_leaves_debug_below_the_floor(self) -> None:
        assert AuditFloorLevel(logging.DEBUG).numeric == logging.DEBUG


class TestRejectsUnknown:
    """An unrecognized level name is a rejected request, not a silent default."""

    @pytest.mark.parametrize("bad", ["loud", "", "verbose", "10"])
    def test_unknown_name_raises_value_error(self, bad: str) -> None:
        with pytest.raises(ValueError, match="unknown log level"):
            AuditFloorLevel.from_name(bad)
