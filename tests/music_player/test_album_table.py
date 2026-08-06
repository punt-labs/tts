"""Tests for AlbumTable: the count label + click-to-play grid, and its selection.

The scene reaches AlbumTable only with a catalogued playing id (PlayerView's T7
guarantee), so these tests drive it directly to pin the two edges the scene cannot
reach: a playing id absent from the table's own albums selects no row, and the
selection names the playing album's friendly key cell.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from punt_vox.voxd.music_player.album_table import AlbumTable
from punt_vox.voxd.programs.album_id import AlbumId

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.voxd.programs.catalog import Album

    type AlbumFactory = Callable[..., Album]


def _table(elements: list[dict[str, object]]) -> dict[str, object]:
    """Return the table element (the second of the label + table pair)."""
    return next(element for element in elements if element["id"] == "music.albums")


def test_no_playing_id_selects_no_row(album_of: AlbumFactory) -> None:
    table = _table(AlbumTable((album_of("aa11bb", name="Techno Mix"),)).elements())

    assert "selected_row_ids" not in table


def test_playing_id_selects_that_albums_key_cell(album_of: AlbumFactory) -> None:
    first = album_of("aa11bb", name="Techno Mix")
    second = album_of("cc22dd", name="Ambient Drift")

    table = _table(AlbumTable((first, second), second.id).elements())

    assert table["selected_row_ids"] == ["Ambient Drift"]


def test_playing_id_absent_from_the_catalog_selects_no_row(
    album_of: AlbumFactory,
) -> None:
    # A playing id the table's own albums do not carry names no row -- the selection
    # stays empty rather than pointing at a row that is not there.
    albums = (album_of("aa11bb", name="Techno Mix"),)
    table = _table(AlbumTable(albums, AlbumId("ffffff")).elements())

    assert "selected_row_ids" not in table
