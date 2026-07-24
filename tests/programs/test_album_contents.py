"""Tests for the ``music get`` value objects: ``PartFile`` and ``AlbumContents``.

Each ``PartFile`` measures itself from disk after bare-name-validating its
identity within the album directory, and ``AlbumContents.from_album`` assembles
the get manifest from a catalog album. The hostile-identity cases prove a
corrupt manifest entry can never stat a file outside the album directory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from punt_vox.voxd.containment import ContainmentRoot
from punt_vox.voxd.programs.album_contents import AlbumContents, PartFile
from punt_vox.voxd.programs.filesystem_store import FilesystemProgramStore
from punt_vox.voxd.programs.part import Part

from .conftest import seed_album

if TYPE_CHECKING:
    from pathlib import Path

_LABEL = "part name"
# Non-empty identities (``Part`` rejects the empty string) that must be refused
# as bare in-root names before any ``stat`` touches the filesystem.
_HOSTILE_IDENTITIES = ["../../../etc/passwd", "a/b.mp3", "..", "n\x00.mp3"]


class TestPartFileMeasured:
    """``PartFile.measured`` sizes a contained part and refuses an escaping one."""

    def test_measures_a_contained_part(self, tmp_path: Path) -> None:
        (tmp_path / "001.mp3").write_bytes(b"audio")
        root = ContainmentRoot(tmp_path, _LABEL)
        part_file = PartFile.measured(root, Part("001.mp3", 1))
        assert part_file.name == "001.mp3"
        assert part_file.byte_count == 5

    @pytest.mark.parametrize("identity", _HOSTILE_IDENTITIES)
    def test_rejects_hostile_identity(self, tmp_path: Path, identity: str) -> None:
        root = ContainmentRoot(tmp_path, _LABEL)
        with pytest.raises(ValueError):
            PartFile.measured(root, Part(identity, 1))


class TestAlbumContentsFromAlbum:
    """``AlbumContents.from_album`` builds the get manifest from a catalog album."""

    def test_builds_from_a_seeded_album(self, tmp_path: Path) -> None:
        root = tmp_path / "programs"
        locator = seed_album(root, 1, 2, name="pads", album_id="a3f1c9")
        album = FilesystemProgramStore(root).scan()[0]

        contents = AlbumContents.from_album(
            album, ContainmentRoot(root / album.locator, _LABEL)
        )

        assert contents.name == locator
        assert [part.name for part in contents.parts] == ["001.mp3", "002.mp3"]
        assert all(part.byte_count == 5 for part in contents.parts)
