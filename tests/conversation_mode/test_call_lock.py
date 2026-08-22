"""Tests for :class:`CallLock`."""

from __future__ import annotations

import os
from pathlib import Path

from punt_vox.voxd.conversation_mode.call_lock import CallLock


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
