"""Tests for :class:`CallControl`."""

from __future__ import annotations

import stat
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


def test_consume_leaves_no_consuming_temp_file_behind(tmp_path: Path) -> None:
    """The atomic take-by-rename must clean up its own ``.consuming`` sibling."""
    path = tmp_path / "call.control"
    control = CallControl(path)
    control.request_stop()
    control.consume()
    assert not path.with_name(path.name + ".consuming").exists()


def test_consume_cleans_up_the_consuming_temp_file_even_on_corrupt_payload(
    tmp_path: Path,
) -> None:
    path = tmp_path / "call.control"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json at all")

    control = CallControl(path)
    control.consume()
    assert not path.with_name(path.name + ".consuming").exists()


def test_consume_treats_invalid_utf8_bytes_as_no_request(tmp_path: Path) -> None:
    """A non-UTF-8 file must not raise UnicodeDecodeError past this boundary.

    AtomicFile.read() decodes with ``encoding="utf-8"``, which raises
    UnicodeDecodeError -- not json.JSONDecodeError/KeyError/TypeError -- on
    invalid bytes. consume()'s own docstring promises a corrupt file is
    "logged and reported as 'no request' rather than raised"; a hand-edited
    or partially-overwritten file with invalid UTF-8 must honor that promise
    too, not propagate to the call-ending boundary handler this method
    exists to spare.
    """
    path = tmp_path / "call.control"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe\x00not valid utf-8")

    control = CallControl(path)
    assert control.consume() is None
    assert not path.exists()


def test_consume_treats_an_unrecognized_kind_as_no_request(tmp_path: Path) -> None:
    """An invalid ``kind`` (e.g. a typo'd hand-edit) must
    be discarded-and-logged, the same as any other malformed entry -- never
    constructed into a ``ControlRequest`` that ``call.py``'s
    ``_apply_control`` would then silently fall through both its branches
    for, dropping a ``/call stop`` with no log and no error.
    """
    path = tmp_path / "call.control"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"kind": "stpo", "target_session_id": null}')

    control = CallControl(path)
    assert control.consume() is None
    # Cleared, not re-read and re-failed forever -- same discipline as
    # every other discard-and-log path in this method.
    assert not path.exists()


def test_consume_treats_a_wrong_typed_target_session_id_as_no_request(
    tmp_path: Path,
) -> None:
    """A malformed ``target_session_id`` (an int, from a hand-edited or
    partially-overwritten file) must be discarded-and-logged, same as an
    unrecognized ``kind`` -- never constructed into a ``ControlRequest`` that
    would crash ``ClaudeSessionAttach``'s constructor downstream.
    """
    path = tmp_path / "call.control"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"kind": "transfer", "target_session_id": 1234}')

    control = CallControl(path)
    assert control.consume() is None
    assert not path.exists()


def test_a_request_written_after_consume_is_not_lost(tmp_path: Path) -> None:
    """The rename-based claim must not leave the mailbox permanently stuck."""
    control = CallControl(tmp_path / "call.control")
    control.request_stop()
    first = control.consume()
    assert first is not None
    assert first.kind == "stop"

    control.request_transfer("session-c")
    second = control.consume()
    assert second is not None
    assert second.kind == "transfer"
    assert second.target_session_id == "session-c"


def test_request_stop_writes_control_file_and_dir_with_restrictive_perms(
    tmp_path: Path,
) -> None:
    """A transfer request's session id is capability-like for --resume --
    the mailbox lands at 0600 under a 0700 directory, not the world-readable
    default."""
    control_dir = tmp_path / "call"
    control = CallControl(control_dir / "call.control")
    control.request_stop()

    assert stat.S_IMODE(control_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((control_dir / "call.control").stat().st_mode) == 0o600
