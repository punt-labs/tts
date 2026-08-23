"""Tests for :class:`CallLock`."""

from __future__ import annotations

import os
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
