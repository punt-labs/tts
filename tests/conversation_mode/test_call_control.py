"""Tests for :class:`CallControl`."""

from __future__ import annotations

from pathlib import Path

from punt_vox.voxd.conversation_mode.call_control import CallControl


def test_no_request_reads_as_none(tmp_path: Path) -> None:
    control = CallControl(tmp_path / "call.control")
    assert control.consume() is None


def test_request_stop_then_consume_returns_a_stop_request(tmp_path: Path) -> None:
    control = CallControl(tmp_path / "call.control")
    control.request_stop()
    request = control.consume()
    assert request is not None
    assert request.kind == "stop"
    assert request.target_session_id is None


def test_request_transfer_then_consume_returns_the_target_session(
    tmp_path: Path,
) -> None:
    control = CallControl(tmp_path / "call.control")
    control.request_transfer("session-b")
    request = control.consume()
    assert request is not None
    assert request.kind == "transfer"
    assert request.target_session_id == "session-b"


def test_request_transfer_with_no_target_re_discovers(tmp_path: Path) -> None:
    control = CallControl(tmp_path / "call.control")
    control.request_transfer(None)
    request = control.consume()
    assert request is not None
    assert request.kind == "transfer"
    assert request.target_session_id is None


def test_consume_clears_the_request(tmp_path: Path) -> None:
    control = CallControl(tmp_path / "call.control")
    control.request_stop()
    control.consume()
    assert control.consume() is None


def test_request_creates_parent_directories(tmp_path: Path) -> None:
    control = CallControl(tmp_path / "nested" / "call.control")
    control.request_stop()
    assert (tmp_path / "nested" / "call.control").exists()


def test_consume_treats_a_corrupt_file_as_no_request(tmp_path: Path) -> None:
    """FR-5's guard: a malformed request must not crash the boundary caller."""
    path = tmp_path / "call.control"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json at all")

    control = CallControl(path)
    assert control.consume() is None
    # The corrupt file is cleared too -- not re-read and re-failed forever.
    assert not path.exists()


def test_consume_treats_a_missing_kind_field_as_no_request(tmp_path: Path) -> None:
    path = tmp_path / "call.control"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"target_session_id": "session-b"}')

    control = CallControl(path)
    assert control.consume() is None
