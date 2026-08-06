"""Tests for AlbumDisplay: one album's per-album cells (id, genre, track count).

The friendly name and the name->album resolution moved to :class:`AlbumNames`
(they need the whole catalog to stay unique and invertible); ``AlbumDisplay``
now owns only the cells derived from a single album.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from punt_vox.voxd.music_player.album_display import AlbumDisplay

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.voxd.programs.catalog import Album

    type AlbumFactory = Callable[..., Album]


def test_cells_come_from_the_manifest(album_of: AlbumFactory) -> None:
    display = AlbumDisplay(album_of("aa11bb", name="Techno Mix", tracks=9))
    assert display.id == "aa11bb"
    assert display.genre == "techno"
    assert display.track_count == 9
