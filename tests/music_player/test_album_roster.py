"""Tests for AlbumRoster: the one live store read on the scene's path.

The roster is where the widget's per-album data leaves the store, so it owns two
properties nothing downstream can restore: the counts are live, and an album the
store has since deleted disappears from *both* the album tuple and the display
tuple, so every region of one render sees one coherent catalog.

The failure it must not have is the quiet one -- a transient ``OSError`` making an
album the user still owns silently vanish from the list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from punt_vox.voxd.music_player.album_roster import AlbumRoster

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.voxd.programs.catalog import Album

    type AlbumFactory = Callable[..., Album]


class TestLiveCounts:
    def test_each_display_carries_its_live_ready_part_count(
        self, album_of: AlbumFactory
    ) -> None:
        albums = (
            album_of("aa11bb", name="One", tracks=0, on_disk=5),
            album_of("cc22dd", name="Two", tracks=0, on_disk=12),
        )
        roster = AlbumRoster.read(albums)
        assert [display.tracks for display in roster.displays] == [5, 12]

    def test_catalog_order_is_preserved(self, album_of: AlbumFactory) -> None:
        albums = (album_of("aa11bb", name="One"), album_of("cc22dd", name="Two"))
        assert AlbumRoster.read(albums).albums == albums


class TestDeletedAlbums:
    def test_a_deleted_album_is_dropped(self, album_of: AlbumFactory) -> None:
        albums = (
            album_of("aa11bb", name="Kept"),
            album_of("cc22dd", name="Gone", fails_with=LookupError("deleted")),
        )
        roster = AlbumRoster.read(albums)
        assert [display.album.id.value for display in roster.displays] == ["aa11bb"]

    def test_albums_and_displays_stay_the_same_length(
        self, album_of: AlbumFactory
    ) -> None:
        albums = (
            album_of("aa11bb", name="Kept"),
            album_of("cc22dd", name="Gone", fails_with=LookupError("deleted")),
        )
        roster = AlbumRoster.read(albums)
        assert len(roster.albums) == len(roster.displays) == 1


class TestRealFaults:
    def test_an_os_error_propagates_rather_than_vanishing_the_album(
        self, album_of: AlbumFactory
    ) -> None:
        albums = (album_of("aa11bb", name="Blip", fails_with=OSError("EMFILE")),)
        with pytest.raises(OSError, match="EMFILE"):
            AlbumRoster.read(albums)
