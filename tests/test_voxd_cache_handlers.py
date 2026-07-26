"""Tests for punt_vox.voxd.cache_handlers -- cache_status and cache_clear.

Each handler holds its cache operation as an injected collaborator, so a test
drives it with a lambda that returns a canned ``CacheInfo`` / count or raises an
``OSError`` -- no daemon, no real cache directory. A fake WebSocket captures the
reply frames. The regression guard for vox-suvs lives at the CLI layer
(``tests/test_cli.py``); here we prove the wire contract: the reply mirrors the
cache functions' return shapes, and a filesystem fault is an id-stamped
operational fault (ERROR "operation failed"), never a silent success or a raise
that tears the connection down.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, cast, final

from punt_vox.cache import CacheInfo
from punt_vox.voxd.cache_handlers import CacheClearHandler, CacheStatusHandler

if TYPE_CHECKING:
    import pytest
    from starlette.websockets import WebSocket


def _capturing_ws() -> tuple[WebSocket, list[dict[str, object]]]:
    sent: list[dict[str, object]] = []

    @final
    class _WS:
        async def send_json(self, payload: dict[str, object]) -> None:
            sent.append(payload)

    return cast("WebSocket", _WS()), sent


class TestCacheStatus:
    def test_reply_mirrors_cache_info(self) -> None:
        info = CacheInfo(entries=3, size_bytes=2048, path=Path("/daemon/cache"))
        ws, sent = _capturing_ws()
        asyncio.run(CacheStatusHandler(lambda: info)({"id": "s1"}, ws))
        assert sent[-1] == {
            "type": "cache_status",
            "id": "s1",
            "entries": 3,
            "size_bytes": 2048,
            "path": "/daemon/cache",
        }

    def test_empty_cache_reports_zeroes(self) -> None:
        info = CacheInfo(entries=0, size_bytes=0, path=Path("/daemon/cache"))
        ws, sent = _capturing_ws()
        asyncio.run(CacheStatusHandler(lambda: info)({"id": "s2"}, ws))
        assert sent[-1]["entries"] == 0
        assert sent[-1]["size_bytes"] == 0

    def test_stat_io_error_is_an_operational_fault(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An OSError sizing the cache is a server-side fault, not a rejection.

        ``cache_status`` sizes each entry with a ``stat``; a permission change
        mid-scan raises ``OSError``. The handler routes it through
        ``WireReply.fault`` -- an id-stamped error frame audited at ERROR
        "operation failed", never WARNING "rejected op" -- and reaches the
        assertions without a raise (the connection stays intact).
        """

        def boom() -> CacheInfo:
            raise OSError("permission denied on cache dir")

        ws, sent = _capturing_ws()
        with caplog.at_level(logging.WARNING, logger="punt_vox.voxd.wire_reply"):
            asyncio.run(CacheStatusHandler(boom)({"id": "s9"}, ws))
        assert sent[-1]["type"] == "error"
        assert sent[-1]["id"] == "s9"
        assert "permission denied" in str(sent[-1]["message"])
        assert any(
            r.levelno == logging.ERROR and "operation failed" in r.getMessage()
            for r in caplog.records
        )
        assert not any("rejected op" in r.getMessage() for r in caplog.records)


class TestCacheClear:
    def test_reply_mirrors_delete_count(self) -> None:
        ws, sent = _capturing_ws()
        asyncio.run(CacheClearHandler(lambda: 7)({"id": "c1"}, ws))
        assert sent[-1] == {"type": "cache_cleared", "id": "c1", "cleared": 7}

    def test_nothing_to_clear_reports_zero(self) -> None:
        ws, sent = _capturing_ws()
        asyncio.run(CacheClearHandler(lambda: 0)({"id": "c2"}, ws))
        assert sent[-1] == {"type": "cache_cleared", "id": "c2", "cleared": 0}

    def test_unlink_io_error_is_an_operational_fault(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A denied unlink (an OSError) faults rather than reporting a false success.

        A partial clear that hit a permission error must not answer "cleared N";
        the handler routes the ``OSError`` through ``WireReply.fault`` (ERROR
        "operation failed") and replies an id-stamped error frame.
        """

        def boom() -> int:
            raise PermissionError("[Errno 13] Permission denied")

        ws, sent = _capturing_ws()
        with caplog.at_level(logging.WARNING, logger="punt_vox.voxd.wire_reply"):
            asyncio.run(CacheClearHandler(boom)({"id": "c9"}, ws))
        assert sent[-1]["type"] == "error"
        assert sent[-1]["id"] == "c9"
        assert "Permission denied" in str(sent[-1]["message"])
        assert any(
            r.levelno == logging.ERROR and "operation failed" in r.getMessage()
            for r in caplog.records
        )
        assert not any("rejected op" in r.getMessage() for r in caplog.records)
