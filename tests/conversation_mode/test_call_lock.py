"""Tests for :class:`CallLock`."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from punt_vox.voxd.conversation_mode.call_lock import CallLock, CallLockActiveError


def test_no_lock_file_reads_as_no_active_call(tmp_path: Path) -> None:
    lock = CallLock(tmp_path / "call.lock")
    assert lock.read() is None


def test_acquire_then_read_reports_reason_and_this_process(tmp_path: Path) -> None:
    lock = CallLock(tmp_path / "call.lock")
    lock.acquire("conversation mode call active")
    state = lock.read()
    assert state is not None
    assert state.reason == "conversation mode call active"
    assert state.pid == os.getpid()


def test_release_clears_the_lock(tmp_path: Path) -> None:
    lock = CallLock(tmp_path / "call.lock")
    lock.acquire("call active")
    lock.release()
    assert lock.read() is None


def test_release_without_a_prior_acquire_is_a_no_op(tmp_path: Path) -> None:
    lock = CallLock(tmp_path / "nested" / "call.lock")
    lock.release()  # must not raise


def test_acquire_creates_parent_directories(tmp_path: Path) -> None:
    lock = CallLock(tmp_path / "nested" / "call.lock")
    lock.acquire("call active")
    assert lock.path.exists()


def test_acquire_raises_when_a_live_process_already_holds_the_lock(
    tmp_path: Path,
) -> None:
    lock = CallLock(tmp_path / "call.lock")
    lock.acquire("first call")  # this test process is genuinely alive

    with pytest.raises(CallLockActiveError) as exc_info:
        lock.acquire("second call")

    assert exc_info.value.state.pid == os.getpid()
    assert exc_info.value.state.reason == "first call"
    assert "vox call stop" in str(exc_info.value)
    # The rejected second acquire must not clobber the first call's lock.
    state = lock.read()
    assert state is not None
    assert state.reason == "first call"


def test_acquire_overwrites_a_stale_lock_from_a_dead_process(tmp_path: Path) -> None:
    lock = CallLock(tmp_path / "call.lock")
    lock.acquire("stale call")

    with patch("os.kill", side_effect=ProcessLookupError):
        lock.acquire("fresh call")  # must not raise: the recorded pid is dead

    state = lock.read()
    assert state is not None
    assert state.reason == "fresh call"
    assert state.pid == os.getpid()


def test_acquire_treats_a_permission_error_as_a_live_process(tmp_path: Path) -> None:
    """A pid owned by another user still proves the process exists."""
    lock = CallLock(tmp_path / "call.lock")
    lock.acquire("owned by someone else")

    with (
        patch("os.kill", side_effect=PermissionError),
        pytest.raises(CallLockActiveError),
    ):
        lock.acquire("second call")

    # The rejected second acquire must not clobber the existing lock.
    state = lock.read()
    assert state is not None
    assert state.reason == "owned by someone else"


def test_read_treats_a_corrupt_lock_file_as_no_active_call(tmp_path: Path) -> None:
    """FR-5's guard: a malformed lock file must not crash the boundary caller."""
    path = tmp_path / "call.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json at all")

    lock = CallLock(path)
    assert lock.read() is None


def test_read_treats_a_missing_pid_field_as_no_active_call(tmp_path: Path) -> None:
    path = tmp_path / "call.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"reason": "call active"}')

    lock = CallLock(path)
    assert lock.read() is None


def test_read_treats_a_wrong_typed_pid_as_no_active_call(tmp_path: Path) -> None:
    """A hand-edited lock file with ``pid`` as a string (valid JSON, wrong
    shape) must be treated as stale, not constructed into a
    :class:`CallLockState` that later crashes with an uncaught ``TypeError``
    from ``os.kill(pid, 0)``.
    """
    path = tmp_path / "call.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"reason": "call active", "pid": "1234"}')

    lock = CallLock(path)
    assert lock.read() is None


def test_read_treats_a_boolean_pid_as_no_active_call(tmp_path: Path) -> None:
    """``bool`` is an ``int`` subclass in Python, so a hand-edited
    ``"pid": true`` would pass a bare ``isinstance(pid, int)`` check and
    coerce to PID 1 in ``os.kill`` -- a real process that always exists,
    owned by another user, producing a permanently stuck "call is active"
    false positive. Must be treated as stale, same as any other
    wrong-typed field.
    """
    path = tmp_path / "call.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"reason": "call active", "pid": true}')

    lock = CallLock(path)
    assert lock.read() is None


def test_is_live_true_for_a_genuinely_live_process(tmp_path: Path) -> None:
    lock = CallLock(tmp_path / "call.lock")
    lock.acquire("call active")  # this test process is genuinely alive
    assert lock.is_live() is True


def test_is_live_false_when_no_lock_file_exists(tmp_path: Path) -> None:
    lock = CallLock(tmp_path / "call.lock")
    assert lock.is_live() is False


def test_is_live_false_for_a_stale_lock_with_a_dead_pid(tmp_path: Path) -> None:
    """Regression: a killed `vox call start` leaves a lock file behind with
    a now-dead pid -- `is_live()` must treat that identically to "no call is
    active", not report a genuinely-dead call as live because the file
    still exists.
    """
    lock = CallLock(tmp_path / "call.lock")
    lock.acquire("stale call")

    with patch("os.kill", side_effect=ProcessLookupError):
        assert lock.is_live() is False


def test_read_treats_invalid_utf8_bytes_as_no_active_call(tmp_path: Path) -> None:
    """A lock file with invalid UTF-8 bytes (corrupted, hand-edited with
    binary garbage) must hit the same discard path as a parse failure --
    not raise ``UnicodeDecodeError`` straight out of :meth:`CallLock.read`,
    which both :meth:`CallLock.acquire` and :meth:`CallLock.is_live` call.
    """
    path = tmp_path / "call.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe\x00\x01garbage")

    lock = CallLock(path)
    assert lock.read() is None


def test_is_live_true_for_a_pid_owned_by_another_user(tmp_path: Path) -> None:
    """A PermissionError still proves the process exists (mirrors acquire())."""
    lock = CallLock(tmp_path / "call.lock")
    lock.acquire("owned by someone else")

    with patch("os.kill", side_effect=PermissionError):
        assert lock.is_live() is True


def test_acquire_writes_the_lock_file_and_directory_with_restrictive_permissions(
    tmp_path: Path,
) -> None:
    """The lock file records a pid/reason -- not itself a secret -- but the
    sibling call.control mailbox carries session ids that are capability-like
    for --resume, so both share this directory's 0700 and land at 0600."""
    lock_dir = tmp_path / "call"
    lock = CallLock(lock_dir / "call.lock")
    lock.acquire("call active")

    assert stat.S_IMODE(lock_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(lock.path.stat().st_mode) == 0o600
