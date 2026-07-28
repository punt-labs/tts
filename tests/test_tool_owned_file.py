"""Tests for the symlink-refusing tool-owned file writer."""

from __future__ import annotations

from pathlib import Path

import pytest

from punt_vox.tool_owned_file import ToolOwnedFile


def test_write_creates_parent_and_file(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir" / "marker"
    ToolOwnedFile(target).write("body\n")
    assert target.read_text(encoding="utf-8") == "body\n"


def test_write_overwrites_wholesale(tmp_path: Path) -> None:
    target = tmp_path / "marker"
    target.write_text("stale longer content\n", encoding="utf-8")
    ToolOwnedFile(target).write("fresh\n")
    # O_TRUNC replaces, never appends or leaves a tail of the old bytes.
    assert target.read_text(encoding="utf-8") == "fresh\n"


def test_write_refuses_symlink_and_leaves_target_untouched(tmp_path: Path) -> None:
    """A symlink at the path is refused; the link target is never overwritten."""
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET\n", encoding="utf-8")
    link = tmp_path / "marker"
    link.symlink_to(secret)

    with pytest.raises(ValueError, match="symlink at a tool-owned path"):
        ToolOwnedFile(link).write("overwrite\n")

    assert link.is_symlink()
    assert secret.read_text(encoding="utf-8") == "TOP SECRET\n"


def test_write_refuses_dangling_symlink(tmp_path: Path) -> None:
    """A symlink to a not-yet-existing path is refused, not materialized."""
    link = tmp_path / "marker"
    link.symlink_to(tmp_path / "does-not-exist")

    with pytest.raises(ValueError, match="symlink at a tool-owned path"):
        ToolOwnedFile(link).write("x\n")

    assert link.is_symlink()
    assert not (tmp_path / "does-not-exist").exists()


def test_is_present_true_for_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "marker"
    target.write_text("x\n", encoding="utf-8")
    assert ToolOwnedFile(target).is_present()


def test_is_present_false_for_symlink(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("x\n", encoding="utf-8")
    link = tmp_path / "marker"
    link.symlink_to(secret)
    # A symlink -- even one pointing at a real file -- is not a legitimate marker.
    assert not ToolOwnedFile(link).is_present()


def test_remove_deletes_symlink_without_following(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("keep\n", encoding="utf-8")
    link = tmp_path / "marker"
    link.symlink_to(secret)

    ToolOwnedFile(link).remove()

    assert not link.exists()
    # unlink removes the link, never its target.
    assert secret.read_text(encoding="utf-8") == "keep\n"


def test_remove_absent_is_a_clean_no_op(tmp_path: Path) -> None:
    ToolOwnedFile(tmp_path / "never-created").remove()
