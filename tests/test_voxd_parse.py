"""Tests for punt_vox.voxd._parse -- wire-frame helpers (parse + safe_send)."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.websockets import WebSocketDisconnect

from punt_vox.voxd._parse import (
    parse_optional_str,
    parse_present_str,
    parse_required_str,
    safe_send,
)

_LOGGER = "punt_vox.voxd._parse"


class TestParseOptionalStr:
    """parse_optional_str rejects a non-string wire value at the boundary."""

    def test_absent_key_is_none(self) -> None:
        assert parse_optional_str({}, "ref") is None

    def test_json_null_is_none(self) -> None:
        assert parse_optional_str({"ref": None}, "ref") is None

    def test_empty_string_is_none(self) -> None:
        assert parse_optional_str({"ref": ""}, "ref") is None

    def test_string_value_passes_through(self) -> None:
        assert parse_optional_str({"ref": "take-1.mp3"}, "ref") == "take-1.mp3"

    def test_non_string_rejected(self) -> None:
        # A number would once have coerced to "5"; now it is a malformed frame.
        with pytest.raises(ValueError, match="ref must be a string, got int"):
            parse_optional_str({"ref": 5}, "ref")

    def test_bool_rejected(self) -> None:
        with pytest.raises(ValueError, match="album must be a string, got bool"):
            parse_optional_str({"album": True}, "album")


class TestParseRequiredStr:
    """parse_required_str rejects a non-string; absent/null yields the empty string."""

    def test_absent_key_is_empty(self) -> None:
        assert parse_required_str({}, "text") == ""

    def test_json_null_is_empty(self) -> None:
        assert parse_required_str({"text": None}, "text") == ""

    def test_empty_string_preserved(self) -> None:
        assert parse_required_str({"text": ""}, "text") == ""

    def test_string_value_passes_through(self) -> None:
        assert parse_required_str({"text": "hello"}, "text") == "hello"

    def test_non_string_rejected(self) -> None:
        # A number would once have coerced to "123"; now it is a malformed frame.
        with pytest.raises(ValueError, match="text must be a string, got int"):
            parse_required_str({"text": 123}, "text")


class TestParsePresentStr:
    """parse_present_str rejects a non-string; keeps "" distinct from absence."""

    def test_absent_key_is_none(self) -> None:
        assert parse_present_str({}, "name") is None

    def test_json_null_is_none(self) -> None:
        assert parse_present_str({"name": None}, "name") is None

    def test_empty_string_preserved_not_collapsed(self) -> None:
        # Unlike parse_optional_str, an explicit "" is kept (a rejectable value),
        # not collapsed to None (absence, which would content-address).
        assert parse_present_str({"name": ""}, "name") == ""

    def test_string_value_passes_through(self) -> None:
        assert parse_present_str({"name": "take-1.mp3"}, "name") == "take-1.mp3"

    def test_non_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="name must be a string, got int"):
            parse_present_str({"name": 123}, "name")


class TestSafeSend:
    """safe_send never lets a client disconnect escape, and logs with context."""

    def test_delivered_returns_true(self) -> None:
        ws = MagicMock()
        ws.send_json = AsyncMock()
        assert asyncio.run(safe_send(ws, {"type": "done", "id": "r1"})) is True

    def test_disconnect_drop_log_carries_frame_context(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A dropped reply is debug-logged with the frame's type and id, no raise."""
        ws = MagicMock()
        ws.send_json = AsyncMock(side_effect=WebSocketDisconnect())

        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            delivered = asyncio.run(
                safe_send(ws, {"type": "audio", "id": "r1", "name": "x.mp3"})
            )

        assert delivered is False  # a normal disconnect is a quiet end, not a raise
        assert "audio" in caplog.text  # which frame
        assert "r1" in caplog.text  # which request
        assert "x.mp3" in caplog.text  # which recording

    def test_runtime_error_drop_log_carries_frame_context(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        ws = MagicMock()
        ws.send_json = AsyncMock(side_effect=RuntimeError("socket closed"))

        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            delivered = asyncio.run(safe_send(ws, {"type": "bytes", "ref": "y.mp3"}))

        assert delivered is False
        assert "bytes" in caplog.text
        assert "y.mp3" in caplog.text
        assert "socket closed" in caplog.text
