"""Tests for LuxTrace: the one ``[lux]``-prefixed logging surface of the lux legs.

Every line the trace emits must carry the ``[lux]`` prefix at the right level and
keep ``%s`` args lazy, so ``grep '\\[lux\\]' vox.log`` replays the whole lifecycle.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from punt_vox.voxd.music_player.lux_trace import LuxTrace

if TYPE_CHECKING:
    import pytest


def test_info_prefixes_lux_and_logs_at_info(caplog: pytest.LogCaptureFixture) -> None:
    trace = LuxTrace(logging.getLogger("test.lux.info"))
    with caplog.at_level(logging.INFO):
        trace.info("connecting to %s", "luxd")
    record = caplog.records[-1]
    assert record.getMessage() == "[lux] connecting to luxd"
    assert record.levelno == logging.INFO


def test_warning_prefixes_lux_and_logs_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    trace = LuxTrace(logging.getLogger("test.lux.warn"))
    with caplog.at_level(logging.WARNING):
        trace.warning("luxd down; retry in %.1fs", 5.0)
    record = caplog.records[-1]
    assert record.getMessage() == "[lux] luxd down; retry in 5.0s"
    assert record.levelno == logging.WARNING


def test_error_prefixes_lux_and_logs_at_error(caplog: pytest.LogCaptureFixture) -> None:
    trace = LuxTrace(logging.getLogger("test.lux.error"))
    with caplog.at_level(logging.ERROR):
        trace.error("rejected %s scene", "vox.music")
    record = caplog.records[-1]
    assert record.getMessage() == "[lux] rejected vox.music scene"
    assert record.levelno == logging.ERROR


def test_args_stay_lazy_not_pre_interpolated(caplog: pytest.LogCaptureFixture) -> None:
    # The template reaches the record unformatted with args attached, so a handler
    # that filters below INFO never pays the interpolation -- the lazy %s contract.
    trace = LuxTrace(logging.getLogger("test.lux.lazy"))
    with caplog.at_level(logging.INFO):
        trace.info("port %s", 8080)
    record = caplog.records[-1]
    assert record.msg == "[lux] port %s"
    assert record.args == (8080,)
