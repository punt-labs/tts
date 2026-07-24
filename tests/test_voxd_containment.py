"""Tests for punt_vox.voxd.containment -- the shared bare-name validator.

Every daemon-owned store resolves a client-supplied bare name through one
``ContainmentRoot``. These tests pin the structural rejections and the
post-resolve containment check once, in the vocabulary of any label, so the
recording store, the play/fetch refs, and the music-part fetch all inherit the
same guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from punt_vox.voxd.containment import ContainmentRoot

# Hostile names no store may ever turn into a path outside its root.
_HOSTILE = [
    "/etc/passwd",
    "../../../etc/cron.d/x",
    "sub/dir/out.mp3",
    "a\\b.mp3",
    "..",
    ".",
    "",
    "bad\x00name.mp3",
    "bad\nname.mp3",
    "tab\tname.mp3",
    "esc\x1bname.mp3",
]


@pytest.fixture
def root(tmp_path: Path) -> ContainmentRoot:
    """A containment root labelled ``part name`` over an isolated directory."""
    return ContainmentRoot(tmp_path / "album", "part name")


class TestStructuralRejections:
    """A hostile bare name is refused before any filesystem touch."""

    def test_absolute_rejected(self, root: ContainmentRoot) -> None:
        with pytest.raises(ValueError, match="absolute"):
            root.resolve("/etc/passwd")

    def test_separator_rejected(self, root: ContainmentRoot) -> None:
        with pytest.raises(ValueError, match="separator"):
            root.resolve("a/b.mp3")
        with pytest.raises(ValueError, match="separator"):
            root.resolve("a\\b.mp3")

    def test_dir_tokens_rejected(self, root: ContainmentRoot) -> None:
        with pytest.raises(ValueError, match="filename"):
            root.resolve("..")
        with pytest.raises(ValueError, match="filename"):
            root.resolve(".")

    def test_empty_rejected(self, root: ContainmentRoot) -> None:
        with pytest.raises(ValueError, match="empty"):
            root.resolve("")

    def test_nul_rejected(self, root: ContainmentRoot) -> None:
        with pytest.raises(ValueError, match="NUL"):
            root.resolve("bad\x00name.mp3")

    def test_non_printable_rejected(self, root: ContainmentRoot) -> None:
        with pytest.raises(ValueError, match="non-printable"):
            root.resolve("bad\nname.mp3")

    @pytest.mark.parametrize("hostile", _HOSTILE)
    def test_every_hostile_name_rejected(
        self, root: ContainmentRoot, hostile: str
    ) -> None:
        """Property: every hostile name raises; none resolves inside the root."""
        with pytest.raises(ValueError):
            root.resolve(hostile)


class TestLabelAndContainment:
    """The label carries into the message; a safe name resolves under the root."""

    def test_label_prefixes_the_message(self, tmp_path: Path) -> None:
        root = ContainmentRoot(tmp_path, "recording name")
        with pytest.raises(ValueError, match="recording name"):
            root.resolve("/etc/passwd")

    def test_safe_name_resolves_within_root(self, root: ContainmentRoot) -> None:
        resolved = root.resolve("part-01.mp3")
        assert resolved == (root.root / "part-01.mp3").resolve()
        assert resolved.is_relative_to(root.root.resolve())

    def test_symlink_escape_rejected(self, tmp_path: Path) -> None:
        """A symlink whose target escapes the root is caught after resolve()."""
        outside = tmp_path / "outside"
        outside.mkdir()
        album = tmp_path / "album"
        album.mkdir()
        (album / "evil.mp3").symlink_to(outside / "secret.mp3")
        root = ContainmentRoot(album, "part name")
        with pytest.raises(ValueError, match="escapes"):
            root.resolve("evil.mp3")
