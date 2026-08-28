"""Tests for AlbumDisplay: one album's per-album cells (id, genre, track count).

The friendly name and the name->album resolution moved to :class:`AlbumNames`
(they need the whole catalog to stay unique and invertible); ``AlbumDisplay``
now owns only the cells derived from a single album.

The track count is the interesting one. It is *held*, read once by
:meth:`AlbumDisplay.read` from the store, never derived from the creation-time
manifest snapshot -- an album is minted with no Parts and filled afterwards, so
the snapshot says zero for its whole life.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from punt_vox.voxd.music_player.album_display import AlbumDisplay

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.voxd.programs.catalog import Album

    type AlbumFactory = Callable[..., Album]


def test_durable_cells_come_from_the_manifest(album_of: AlbumFactory) -> None:
    display = AlbumDisplay.read(album_of("aa11bb", name="Techno Mix", tracks=9))
    assert display.id == "aa11bb"
    assert display.genre == "techno"


def test_read_counts_ready_parts_not_the_manifest_snapshot(
    album_of: AlbumFactory,
) -> None:
    # The reported defect's exact shape: minted with an empty snapshot, five
    # Parts on disk. The cell must read 5, not 0.
    album = album_of("aa11bb", name="Techno Mix", tracks=0, on_disk=5)
    assert AlbumDisplay.read(album).tracks == 5


def test_read_raises_when_the_store_no_longer_holds_the_album(
    album_of: AlbumFactory,
) -> None:
    album = album_of("999001", name="Gone", fails_with=LookupError("deleted"))
    with pytest.raises(LookupError):
        AlbumDisplay.read(album)
