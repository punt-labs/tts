"""Tests for :mod:`punt_vox.panel.hub_outage_log`."""

from __future__ import annotations

import logging
import threading
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


class TestConcurrency:
    def test_concurrent_note_calls_serialize_instead_of_racing(self) -> None:
        """Two threads calling note() must not read/write _started_at at once.

        note() and clear() are called from both the leg's event-loop
        coroutine and its to_thread worker on the same instance. Without a
        lock, a second caller could read _started_at as None (or have it
        nulled by a concurrent clear()) mid-computation and both log a
        first-tick WARNING for what should be one continuing outage.
        """
        logger = _mock_logger()
        entered = threading.Event()
        release = threading.Event()

        def _slow_warning(*args: object, **kwargs: object) -> None:
            entered.set()
            release.wait(timeout=2)

        logger.warning.side_effect = _slow_warning
        outage = HubOutageLog(logger)

        first = threading.Thread(target=outage.note, args=("down",))
        first.start()
        assert entered.wait(timeout=2)

        # A second caller must block on the lock, not run concurrently and
        # race the first thread's read-modify-write of _started_at.
        second = threading.Thread(target=outage.note, args=("still down",))
        second.start()
        second.join(timeout=0.2)
        assert second.is_alive(), "a second caller got in while the first held the lock"

        release.set()
        first.join(timeout=2)
        second.join(timeout=2)

        # The second call, having waited for the first, sees _started_at
        # already set, so it logs at DEBUG rather than a second WARNING.
        assert logger.warning.call_count == 1
