"""Tests for punt_vox.voxd.log_level_handler -- the set_log_level wire op.

The handler holds its level-applier as an injected collaborator, so a test drives
it with a recorder instead of the process root logger. These pin the wire
contract: a valid level is applied (clamped to the INFO audit floor) and echoed
back, a sub-INFO request is clamped rather than honored, and an unknown level is
a rejected client request that applies nothing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast, final

from punt_vox.voxd.log_level_handler import LogLevelHandler

if TYPE_CHECKING:
    from starlette.websockets import WebSocket


def _capturing_ws() -> tuple[WebSocket, list[dict[str, object]]]:
    sent: list[dict[str, object]] = []

    @final
    class _WS:
        async def send_json(self, payload: dict[str, object]) -> None:
            sent.append(payload)

    return cast("WebSocket", _WS()), sent


def _handler() -> tuple[LogLevelHandler, list[int]]:
    """Return a handler and the list of numeric levels its applier received."""
    applied: list[int] = []
    return LogLevelHandler(applied.append), applied


class TestAppliesValidLevel:
    def test_debug_is_applied_and_echoed(self) -> None:
        handler, applied = _handler()
        ws, sent = _capturing_ws()
        asyncio.run(handler({"id": "l1", "level": "debug"}, ws))
        assert applied == [logging.DEBUG]
        assert sent[-1] == {"type": "log_level", "id": "l1", "level": "debug"}

    def test_info_is_applied_and_echoed(self) -> None:
        handler, applied = _handler()
        ws, sent = _capturing_ws()
        asyncio.run(handler({"id": "l2", "level": "info"}, ws))
        assert applied == [logging.INFO]
        assert sent[-1] == {"type": "log_level", "id": "l2", "level": "info"}


class TestInfoFloor:
    def test_sub_info_request_is_clamped_not_honored(self) -> None:
        """A crafted `warning` frame applies INFO, not WARNING -- the audit floor.

        The daemon must never drop below INFO on a client's say-so, or the trail
        that records the client's own rejected requests goes dark.
        """
        handler, applied = _handler()
        ws, sent = _capturing_ws()
        asyncio.run(handler({"id": "l3", "level": "warning"}, ws))
        assert applied == [logging.INFO]  # clamped down, never WARNING
        assert sent[-1] == {"type": "log_level", "id": "l3", "level": "info"}


class TestRejectsUnknown:
    def test_unknown_level_is_rejected_and_applies_nothing(self) -> None:
        handler, applied = _handler()
        ws, sent = _capturing_ws()
        asyncio.run(handler({"id": "l4", "level": "loud"}, ws))
        assert applied == []  # a rejected request changes no level
        assert sent[-1]["type"] == "error"
        assert sent[-1]["id"] == "l4"
        assert "unknown log level" in str(sent[-1]["message"])

    def test_missing_level_is_rejected(self) -> None:
        handler, applied = _handler()
        ws, sent = _capturing_ws()
        asyncio.run(handler({"id": "l5"}, ws))
        assert applied == []
        assert sent[-1]["type"] == "error"
