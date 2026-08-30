"""Tests for TrackCountCache: the one place left that reads a track count from disk.

:meth:`refresh` is the blocking call this cache exists to isolate -- callers are
required to run it via ``asyncio.to_thread`` (proved in ``test_player.py``, where
the caller lives); this module tests its own behavior in isolation: an
unrefreshed album reads as zero, a refresh picks up the live count, a deleted
album drops out of the refreshed set, and a genuine fault propagates rather than
freezing that album's count silently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from punt_vox.voxd.music_player.track_count_cache import TrackCountCache
from punt_vox.voxd.programs.album_id import AlbumId

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.voxd.programs.catalog import Album

    type AlbumFactory = Callable[..., Album]


class TestGet:
    def test_an_unrefreshed_album_reads_as_zero(self) -> None:
        assert TrackCountCache().get(AlbumId("aa11bb")) == 0


class TestRefresh:
    def test_each_album_gets_its_live_ready_part_count(
        self, album_of: AlbumFactory
    ) -> None:
        one = album_of("aa11bb", name="One", tracks=0, on_disk=5)
        two = album_of("cc22dd", name="Two", tracks=0, on_disk=12)
        cache = TrackCountCache()

        cache.refresh((one, two))

        assert cache.get(one.id) == 5
        assert cache.get(two.id) == 12

    def test_a_later_refresh_replaces_the_earlier_counts(
        self, album_of: AlbumFactory
    ) -> None:
        album = album_of("aa11bb", tracks=0, on_disk=3)
        cache = TrackCountCache()
        cache.refresh((album,))
        assert cache.get(album.id) == 3

        grown = album_of("aa11bb", tracks=0, on_disk=7)
        cache.refresh((grown,))
        assert cache.get(album.id) == 7

    def test_a_deleted_album_drops_out_of_the_refreshed_set(
        self, album_of: AlbumFactory
    ) -> None:
        kept = album_of("aa11bb", name="Kept", tracks=0, on_disk=4)
        gone = album_of("cc22dd", name="Gone", fails_with=LookupError("deleted"))
        cache = TrackCountCache()

        cache.refresh((kept, gone))

        assert cache.get(kept.id) == 4
        assert cache.get(gone.id) == 0  # never cached, not a real zero

    def test_a_deleted_album_does_not_evict_a_stale_but_still_valid_entry(
        self, album_of: AlbumFactory
    ) -> None:
        # A prior refresh cached Gone's count; a later refresh that fails for
        # Gone alone must not wipe Kept's entry -- the whole dict is rebuilt
        # fresh each refresh, per-album, not merged.
        kept = album_of("aa11bb", name="Kept", tracks=0, on_disk=4)
        first_pass = album_of("cc22dd", name="Gone", tracks=0, on_disk=2)
        cache = TrackCountCache()
        cache.refresh((kept, first_pass))
        assert cache.get(kept.id) == 4
        assert cache.get(first_pass.id) == 2

        gone = album_of("cc22dd", name="Gone", fails_with=LookupError("deleted"))
        cache.refresh((kept, gone))

        assert cache.get(kept.id) == 4
        assert cache.get(gone.id) == 0

    def test_a_real_fault_propagates_rather_than_freezing_the_count(
        self, album_of: AlbumFactory
    ) -> None:
        album = album_of("aa11bb", name="Blip", fails_with=OSError("EMFILE"))
        with pytest.raises(OSError, match="EMFILE"):
            TrackCountCache().refresh((album,))
