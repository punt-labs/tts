"""Tests for punt_vox.voxd.path_status -- absence vs access-fault classification.

The load-bearing property: a benign missing path (``ENOENT``) classifies as an
absent status whose predicates are all False, but any *other* ``OSError`` --
``PermissionError`` above all -- propagates from :meth:`PathStatus.of`, so a store
path that exists but cannot be read surfaces the fault instead of masquerading as
absent. This is the primitive the store/catalog surface leans on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from punt_vox.voxd.path_status import PathStatus


class TestClassification:
    """A single stat classifies the path's kind, following symlinks by default."""

    def test_directory(self, tmp_path: Path) -> None:
        status = PathStatus.of(tmp_path)
        assert status.is_directory
        assert not status.is_regular_file
        assert not status.is_symlink

    def test_regular_file(self, tmp_path: Path) -> None:
        target = tmp_path / "a.mp3"
        target.write_bytes(b"1234")
        status = PathStatus.of(target)
        assert status.is_regular_file
        assert not status.is_directory
        assert not status.is_symlink

    def test_symlink_followed_by_default(self, tmp_path: Path) -> None:
        target = tmp_path / "real.mp3"
        target.write_bytes(b"1234")
        link = tmp_path / "link.mp3"
        link.symlink_to(target)
        # Default follow: the link resolves to its regular-file target.
        status = PathStatus.of(link)
        assert status.is_regular_file
        assert not status.is_symlink

    def test_symlink_no_follow_is_the_link(self, tmp_path: Path) -> None:
        target = tmp_path / "real.mp3"
        target.write_bytes(b"1234")
        link = tmp_path / "link.mp3"
        link.symlink_to(target)
        status = PathStatus.of(link, follow_symlinks=False)
        assert status.is_symlink
        assert not status.is_regular_file

    def test_broken_symlink_no_follow_is_the_link(self, tmp_path: Path) -> None:
        link = tmp_path / "dangling.mp3"
        link.symlink_to(tmp_path / "does-not-exist.mp3")
        status = PathStatus.of(link, follow_symlinks=False)
        assert status.is_symlink


class TestAbsence:
    """A missing path is the one benign miss: an absent status, not a fault."""

    def test_absent_path_has_no_kind(self, tmp_path: Path) -> None:
        status = PathStatus.of(tmp_path / "nope")
        assert not status.is_directory
        assert not status.is_regular_file
        assert not status.is_symlink

    def test_broken_symlink_followed_reads_as_absent(self, tmp_path: Path) -> None:
        # Following a dangling link hits ENOENT on the target -- benign absence,
        # never a fault.
        link = tmp_path / "dangling.mp3"
        link.symlink_to(tmp_path / "does-not-exist.mp3")
        status = PathStatus.of(link)
        assert not status.is_regular_file
        assert not status.is_directory


class TestAccessFaultPropagates:
    """A non-ENOENT OSError is an access fault and must never be swallowed."""

    def test_permission_error_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "guarded"

        def denied(_self: Path, *, follow_symlinks: bool = True) -> object:
            raise PermissionError(13, "permission denied")

        monkeypatch.setattr(Path, "stat", denied)
        with pytest.raises(PermissionError, match="permission denied"):
            PathStatus.of(target)
