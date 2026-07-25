"""Tests for punt_vox.voxd.wire_reply -- id-stamped sends and logged rejections."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from starlette.websockets import WebSocketDisconnect

from punt_vox.voxd.wire_reply import WireReply

if TYPE_CHECKING:
    import pytest


def _capturing_ws() -> tuple[MagicMock, list[dict[str, object]]]:
    sent: list[dict[str, object]] = []

    async def _send(payload: dict[str, object]) -> None:
        sent.append(payload)

    ws = MagicMock()
    ws.send_json = AsyncMock(side_effect=_send)
    return ws, sent


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.WARNING]


def _errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.ERROR]


class TestSend:
    """send stamps the request id and survives a vanished peer."""

    def test_stamps_request_id(self) -> None:
        ws, sent = _capturing_ws()
        delivered = asyncio.run(WireReply(ws, "r1").send({"type": "done"}))
        assert delivered is True
        assert sent == [{"id": "r1", "type": "done"}]

    def test_returns_false_when_peer_gone(self) -> None:
        ws = MagicMock()
        ws.send_json = AsyncMock(side_effect=WebSocketDisconnect())
        delivered = asyncio.run(WireReply(ws, "r1").send({"type": "done"}))
        assert delivered is False

    def test_stamp_overrides_payload_id(self) -> None:
        """A payload carrying its own 'id' cannot override the stamped request id."""
        ws, sent = _capturing_ws()
        asyncio.run(WireReply(ws, "real").send({"type": "x", "id": "forged"}))
        assert sent[-1]["id"] == "real"


class TestErrorLogging:
    """error audit-logs the rejection at WARNING and sends the error frame."""

    def test_logs_one_warning_with_request_id(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        ws, sent = _capturing_ws()
        with caplog.at_level(logging.WARNING):
            asyncio.run(WireReply(ws, "req-42").error("name must not be absolute"))
        records = _warnings(caplog)
        assert len(records) == 1
        assert "req-42" in records[0].getMessage()
        assert sent[-1] == {
            "id": "req-42",
            "type": "error",
            "message": "name must not be absolute",
        }

    def test_wire_message_is_verbatim_but_log_has_no_raw_control_chars(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        ws, sent = _capturing_ws()
        hostile = "evil\nINJECTED forged log line\r\tname"
        with caplog.at_level(logging.WARNING):
            asyncio.run(WireReply(ws, "r1").error(hostile))
        # The client frame carries the message verbatim; nothing is stripped.
        assert sent[-1]["message"] == hostile
        # The log line escapes the control characters -- no injection into vox.log.
        logged = _warnings(caplog)[-1].getMessage()
        assert "\n" not in logged
        assert "\r" not in logged
        assert "\t" not in logged
        assert "\\n" in logged

    def test_long_message_is_capped_in_the_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        ws, _sent = _capturing_ws()
        with caplog.at_level(logging.WARNING):
            asyncio.run(WireReply(ws, "r1").error("A" * 500))
        logged = _warnings(caplog)[-1].getMessage()
        assert logged.endswith("...")
        # A bounded (~120 char) field plus the id prefix, never the full 500.
        assert len(logged) < 200

    def test_logs_even_when_peer_gone(self, caplog: pytest.LogCaptureFixture) -> None:
        ws = MagicMock()
        ws.send_json = AsyncMock(side_effect=WebSocketDisconnect())
        with caplog.at_level(logging.WARNING):
            delivered = asyncio.run(WireReply(ws, "r1").error("gone"))
        assert delivered is False
        # The audit trail does not depend on the client still being connected.
        assert _warnings(caplog)

    def test_rejection_is_labeled_rejected_op(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A malformed-input rejection audits as 'rejected op', not a fault."""
        ws, _sent = _capturing_ws()
        with caplog.at_level(logging.WARNING):
            asyncio.run(WireReply(ws, "r1").error("name must not be absolute"))
        logged = _warnings(caplog)[-1].getMessage()
        assert "rejected op" in logged
        assert "operation failed" not in logged
        assert not _errors(caplog)


class TestFaultLogging:
    """fault audit-logs a server-side operational failure at ERROR, not a rejection."""

    def test_logs_operation_failed_at_error_with_request_id(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        ws, _sent = _capturing_ws()
        with caplog.at_level(logging.ERROR):
            delivered = asyncio.run(WireReply(ws, "req-9").fault("synthesis failed"))
        assert delivered is True
        records = _errors(caplog)
        assert len(records) == 1
        message = records[0].getMessage()
        assert "operation failed" in message
        assert "rejected op" not in message
        assert "req-9" in message

    def test_client_frame_is_identical_to_error(self) -> None:
        """The wire frame a fault sends is byte-for-byte the one error sends."""
        fault_ws, fault_sent = _capturing_ws()
        error_ws, error_sent = _capturing_ws()
        asyncio.run(WireReply(fault_ws, "r1").fault("boom"))
        asyncio.run(WireReply(error_ws, "r1").error("boom"))
        assert fault_sent == error_sent
        assert fault_sent[-1] == {"id": "r1", "type": "error", "message": "boom"}

    def test_not_labeled_rejected_op(self, caplog: pytest.LogCaptureFixture) -> None:
        """A server fault never audits at WARNING as a client rejection."""
        ws, _sent = _capturing_ws()
        with caplog.at_level(logging.WARNING):
            asyncio.run(WireReply(ws, "r1").fault("store write failed"))
        assert not _warnings(caplog)

    def test_message_is_sanitized_in_the_log_but_verbatim_on_the_wire(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        ws, sent = _capturing_ws()
        hostile = "boom\nINJECTED\r\tfault"
        with caplog.at_level(logging.ERROR):
            asyncio.run(WireReply(ws, "r1").fault(hostile))
        assert sent[-1]["message"] == hostile
        logged = _errors(caplog)[-1].getMessage()
        assert "\n" not in logged
        assert "\\n" in logged

    def test_logs_even_when_peer_gone(self, caplog: pytest.LogCaptureFixture) -> None:
        ws = MagicMock()
        ws.send_json = AsyncMock(side_effect=WebSocketDisconnect())
        with caplog.at_level(logging.ERROR):
            delivered = asyncio.run(WireReply(ws, "r1").fault("gone"))
        assert delivered is False
        assert _errors(caplog)


class TestRejectOrFault:
    """reject_or_fault routes ValueError to error, LookupError/OSError to fault."""

    def test_value_error_audits_rejected_op(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        ws, sent = _capturing_ws()
        with caplog.at_level(logging.WARNING):
            asyncio.run(WireReply(ws, "r1").reject_or_fault(ValueError("bad field")))
        logged = _warnings(caplog)[-1].getMessage()
        assert "rejected op" in logged
        assert not _errors(caplog)
        assert sent[-1] == {"id": "r1", "type": "error", "message": "bad field"}

    def test_lookup_error_audits_operation_failed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        ws, _sent = _capturing_ws()
        with caplog.at_level(logging.WARNING):
            asyncio.run(WireReply(ws, "r1").reject_or_fault(LookupError("gone dir")))
        assert "operation failed" in _errors(caplog)[-1].getMessage()
        assert not _warnings(caplog)

    def test_os_error_audits_operation_failed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        ws, _sent = _capturing_ws()
        with caplog.at_level(logging.WARNING):
            asyncio.run(WireReply(ws, "r1").reject_or_fault(OSError("disk full")))
        assert "operation failed" in _errors(caplog)[-1].getMessage()
        assert not _warnings(caplog)
