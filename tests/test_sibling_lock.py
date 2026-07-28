"""Tests for the sibling-file exclusive lock (``SiblingLock``)."""

from __future__ import annotations

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

    # An exclusive acquire then a release, both on the sibling -- never the host.
    assert ops[0] == (lock.path, fcntl.LOCK_EX)
    assert ops[-1] == (lock.path, fcntl.LOCK_UN)
    assert all(path == lock.path for path, _ in ops)
    assert host not in [path for path, _ in ops]


def test_held_creates_missing_parent_directory(tmp_path: Path) -> None:
    host = tmp_path / "nested" / "dir" / "CLAUDE.md"
    lock = SiblingLock(host)
    with lock.held():
        assert lock.path.exists()


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
