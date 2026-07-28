"""Tests for the sibling-file exclusive lock (``SiblingLock``)."""

from __future__ import annotations

import errno
import fcntl
from pathlib import Path
from typing import IO

import pytest

from punt_vox.sibling_lock import SiblingLock


def test_lock_path_is_the_hidden_sibling(tmp_path: Path) -> None:
    host = tmp_path / "CLAUDE.md"
    # The lock is a hidden sibling in the host's own directory, tool-agnostic.
    assert SiblingLock(host).path == tmp_path / ".CLAUDE.md.punt-import.lock"


def test_held_creates_the_lock_and_flocks_it_exclusively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = tmp_path / "CLAUDE.md"
    ops: list[tuple[Path, int]] = []
    real_flock = fcntl.flock

    def spy_flock(fileobj: IO[str], operation: int) -> None:
        ops.append((Path(fileobj.name), operation))
        real_flock(fileobj, operation)

    monkeypatch.setattr("punt_vox.sibling_lock.fcntl.flock", spy_flock)
    lock = SiblingLock(host)
    with lock.held():
        assert lock.path.exists()

    # A non-blocking exclusive acquire then a release, both on the sibling --
    # never the host. The acquire is LOCK_EX | LOCK_NB so contention cannot hang.
    assert ops[0] == (lock.path, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert ops[-1] == (lock.path, fcntl.LOCK_UN)
    assert all(path == lock.path for path, _ in ops)
    assert host not in [path for path, _ in ops]


def test_held_creates_missing_parent_directory(tmp_path: Path) -> None:
    host = tmp_path / "nested" / "dir" / "CLAUDE.md"
    lock = SiblingLock(host)
    with lock.held():
        assert lock.path.exists()


def test_acquire_times_out_and_names_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A wedged peer must not hang the caller forever: the non-blocking acquire
    # polls a bounded number of times, then raises a clear error naming the host.
    host = tmp_path / "CLAUDE.md"
    lock = SiblingLock(host)

    def always_blocked(_fileobj: IO[str], _operation: int) -> None:
        raise BlockingIOError

    sleeps: list[float] = []

    def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("punt_vox.sibling_lock.fcntl.flock", always_blocked)
    monkeypatch.setattr("punt_vox.sibling_lock.time.sleep", record_sleep)

    with pytest.raises(TimeoutError, match=r"CLAUDE\.md"), lock.held():
        pass

    # One sleep per failed attempt, bounded by the acquire budget.
    assert len(sleeps) == SiblingLock._ACQUIRE_ATTEMPTS


def test_non_contention_oserror_propagates_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A non-contention flock failure -- ENOLCK, an flock-less filesystem, EBADF --
    # is a genuine error, not a wedged peer. It must propagate immediately with its
    # true cause, never be retried and relabelled as a false TimeoutError.
    lock = SiblingLock(tmp_path / "CLAUDE.md")

    def no_locks(_fileobj: IO[str], _operation: int) -> None:
        raise OSError(errno.ENOLCK, "no locks available")

    sleeps: list[float] = []

    def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("punt_vox.sibling_lock.fcntl.flock", no_locks)
    monkeypatch.setattr("punt_vox.sibling_lock.time.sleep", record_sleep)

    with pytest.raises(OSError) as caught, lock.held():
        pass

    # The true errno survives; it is not a TimeoutError, and no retry happened.
    assert caught.value.errno == errno.ENOLCK
    assert not isinstance(caught.value, TimeoutError)
    assert sleeps == []


def test_held_releases_on_exception(tmp_path: Path) -> None:
    # The finally-branch releases even when the with-body raises, so a failed
    # RMW never leaves the lock held.
    lock = SiblingLock(tmp_path / "CLAUDE.md")
    with pytest.raises(ValueError, match="boom"), lock.held():
        raise ValueError("boom")
    # A second acquisition would block forever if the first were never released;
    # that it returns proves the lock was freed.
    with lock.held():
        assert lock.path.exists()
