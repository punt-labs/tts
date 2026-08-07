"""Tests for :mod:`punt_vox.panel.hub_outage_log`."""

from __future__ import annotations

import logging
from unittest.mock import Mock

from punt_vox.panel.hub_outage_log import HubOutageLog


def _mock_logger() -> Mock:
    return Mock(spec=logging.Logger)


class TestNote:
    def test_first_tick_logs_at_warning(self) -> None:
        logger = _mock_logger()
        HubOutageLog(logger).note("down")
        logger.warning.assert_called_once()
        logger.debug.assert_not_called()
        logger.info.assert_not_called()

    def test_second_tick_shortly_after_stays_at_debug(self) -> None:
        logger = _mock_logger()
        outage = HubOutageLog(logger)
        outage.note("down")
        outage.note("down")
        logger.warning.assert_called_once()
        logger.debug.assert_called_once()
        logger.info.assert_not_called()

    def test_a_tick_after_the_restate_window_logs_at_info(self) -> None:
        logger = _mock_logger()
        outage = HubOutageLog(logger)
        outage.note("down")
        # Force the next tick to look like it happened 31s later.
        outage._last_logged_at -= 31.0  # test drives internal timing directly
        outage.note("down")
        logger.info.assert_called_once()
        logger.debug.assert_not_called()


class TestClear:
    def test_clearing_then_a_new_outage_logs_at_warning_again(self) -> None:
        logger = _mock_logger()
        outage = HubOutageLog(logger)
        outage.note("down")
        outage.clear()
        outage.note("down")
        assert logger.warning.call_count == 2
