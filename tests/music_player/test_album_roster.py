"""Tests for AlbumRoster: pairs the catalog with cached track counts, no disk.

``from_cache`` never touches the store -- the disk read that used to happen
inline here now lives in
:class:`~punt_vox.voxd.music_player.track_count_cache.TrackCountCache` (see
``test_track_count_cache.py``), refreshed off the hot path. This module tests
only the pairing: cached counts land on the right album, in catalog order, and
an album the cache has never seen reads as zero rather than raising or being
dropped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from punt_vox.voxd.music_player.album_roster import AlbumRoster
from punt_vox.voxd.music_player.track_count_cache import TrackCountCache

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.voxd.programs.catalog import Album

    type AlbumFactory = Callable[..., Album]


class TestFromCache:
    def test_each_display_carries_its_cached_track_count(
        self, album_of: AlbumFactory
    ) -> None:
        # Distinct counts on purpose: two albums both defaulting to 3 tracks
        # would pass this assertion even paired backwards -- [3, 3] == [3, 3]
        # either way. One and Two must disagree so a rotated pairing in
        # ``from_cache`` actually fails here.
        one = album_of("aa11bb", name="One", on_disk=3)
        two = album_of("cc22dd", name="Two", on_disk=5)
        cache = TrackCountCache.for_testing((one, two))

        roster = AlbumRoster.from_cache((one, two), cache)

        assert [display.tracks for display in roster.displays] == [
            cache.get(one.id),
            cache.get(two.id),
        ]
        assert [display.tracks for display in roster.displays] == [3, 5]

    def test_catalog_order_is_preserved(self, album_of: AlbumFactory) -> None:
        albums = (album_of("aa11bb", name="One"), album_of("cc22dd", name="Two"))
        assert AlbumRoster.from_cache(albums, TrackCountCache()).albums == albums

    def test_an_unrefreshed_album_reads_as_a_zero_track_count(
        self, album_of: AlbumFactory
    ) -> None:
        # No disk touched here: an album this cache has never refreshed simply
        # reads as zero rather than raising or being dropped from the roster.
        album = album_of("aa11bb", name="Fresh", on_disk=9)
        roster = AlbumRoster.from_cache((album,), TrackCountCache())

        assert roster.albums == (album,)
        assert roster.displays[0].tracks == 0

    def test_albums_and_displays_stay_the_same_length(
        self, album_of: AlbumFactory
    ) -> None:
        albums = (album_of("aa11bb"), album_of("cc22dd"))
        roster = AlbumRoster.from_cache(albums, TrackCountCache())
        assert len(roster.albums) == len(roster.displays) == 2
