"""Tests for AlbumTable: the count label + click-to-play grid, and its selection.

The scene reaches AlbumTable only with a catalogued playing id (PlayerView's T7
guarantee), so these tests drive it directly to pin the two edges the scene cannot
reach: a playing id absent from the table's own albums selects no row, and the
selection names the playing album's friendly key cell.

``selected_row_ids`` is always emitted, empty list and all. That is not cosmetic:
a key present in one render and gone from the next is a shape change no patch can
express, so a vanishing selection would leave the stale row highlighted until
something forced a full re-install.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from punt_vox.voxd.music_player.album_roster import AlbumRoster
from punt_vox.voxd.music_player.album_table import AlbumTable
from punt_vox.voxd.music_player.track_count_cache import TrackCountCache
from punt_vox.voxd.programs.album_id import AlbumId

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.voxd.programs.catalog import Album

    type AlbumFactory = Callable[..., Album]


def _table(elements: list[dict[str, object]]) -> dict[str, object]:
    """Return the table element (the second of the label + table pair)."""
    return next(element for element in elements if element["id"] == "music.albums")


def _elements(
    albums: tuple[Album, ...], playing: AlbumId | None = None
) -> dict[str, object]:
    cache = TrackCountCache()
    cache.refresh(albums)
    roster = AlbumRoster.from_cache(albums, cache)
    return _table(AlbumTable(roster, playing).elements())


def test_no_playing_id_selects_no_row(album_of: AlbumFactory) -> None:
    assert _elements((album_of("aa11bb", name="Techno Mix"),))["selected_row_ids"] == []


def test_playing_id_selects_that_albums_key_cell(album_of: AlbumFactory) -> None:
    first = album_of("aa11bb", name="Techno Mix")
    second = album_of("cc22dd", name="Ambient Drift")

    table = _elements((first, second), second.id)

    assert table["selected_row_ids"] == ["Ambient Drift"]


def test_playing_id_absent_from_the_catalog_selects_no_row(
    album_of: AlbumFactory,
) -> None:
    # A playing id the table's own albums do not carry names no row -- the selection
    # empties rather than pointing at a row that is not there.
    albums = (album_of("aa11bb", name="Techno Mix"),)

    assert _elements(albums, AlbumId("ffffff"))["selected_row_ids"] == []


def test_rows_precede_the_selection_in_the_wire_dict(album_of: AlbumFactory) -> None:
    # lux's setters run in this order on a patch, and the selection is intersected
    # against the rows as they stand at setter time -- selection first would drop
    # every id the old row set lacked.
    table = _elements((album_of("aa11bb", name="Techno Mix"),))
    fields = list(table)

    assert fields.index("rows") < fields.index("selected_row_ids")


def test_the_tracks_cell_reads_the_live_count(album_of: AlbumFactory) -> None:
    albums = (album_of("aa11bb", name="Techno Mix", tracks=0, on_disk=7),)
    rows = _elements(albums)["rows"]

    assert rows == [["Techno Mix", "techno", 7]]
