"""Tests for TrackCountCache: the one place left that reads a track count from disk.

:meth:`_refresh` is the blocking call this cache exists to isolate -- callers are
required to run it via ``asyncio.to_thread`` (proved in ``test_player.py``, where
the caller lives); this module tests its own behavior in isolation: an
unrefreshed album reads as zero, a refresh picks up the live count, a deleted
album drops out of the refreshed set, and a genuine fault propagates rather than
freezing that album's count silently.

Also covered here: the two safety properties added around the disk read --
:meth:`serialized_refresh` gives up on a stuck refresh after a bounded timeout
rather than blocking its caller forever, and a write from an abandoned refresh
that finally lands after that timeout cannot clobber a newer refresh's result.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import TYPE_CHECKING

import pytest

from punt_vox.voxd.music_player import track_count_cache as track_count_cache_module
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

        cache._generation += 1
        cache._refresh((one, two))

        assert cache.get(one.id) == 5
        assert cache.get(two.id) == 12

    def test_a_later_refresh_replaces_the_earlier_counts(
        self, album_of: AlbumFactory
    ) -> None:
        album = album_of("aa11bb", tracks=0, on_disk=3)
        cache = TrackCountCache()
        cache._generation += 1
        cache._refresh((album,))
        assert cache.get(album.id) == 3

        grown = album_of("aa11bb", tracks=0, on_disk=7)
        cache._generation += 1
        cache._refresh((grown,))
        assert cache.get(album.id) == 7

    def test_a_deleted_album_drops_out_of_the_refreshed_set(
        self, album_of: AlbumFactory
    ) -> None:
        kept = album_of("aa11bb", name="Kept", tracks=0, on_disk=4)
        gone = album_of("cc22dd", name="Gone", fails_with=LookupError("deleted"))
        cache = TrackCountCache()

        cache._generation += 1
        cache._refresh((kept, gone))

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
        cache._generation += 1
        cache._refresh((kept, first_pass))
        assert cache.get(kept.id) == 4
        assert cache.get(first_pass.id) == 2

        gone = album_of("cc22dd", name="Gone", fails_with=LookupError("deleted"))
        cache._generation += 1
        cache._refresh((kept, gone))

        assert cache.get(kept.id) == 4
        assert cache.get(gone.id) == 0

    def test_a_real_fault_propagates_rather_than_freezing_the_count(
        self, album_of: AlbumFactory
    ) -> None:
        album = album_of("aa11bb", name="Blip", fails_with=OSError("EMFILE"))
        cache = TrackCountCache()
        cache._generation += 1
        with pytest.raises(OSError, match="EMFILE"):
            cache._refresh((album,))


class TestGenerationGatedWrite:
    """The write inside :meth:`_refresh` only lands for the newest generation.

    This is what keeps a write from an abandoned (timed-out) refresh from
    clobbering a newer one that already completed -- see
    ``TestSerializedRefreshTimeout`` below for the same property exercised
    through the real async path.
    """

    def test_an_older_generation_writing_late_does_not_clobber_a_newer_one(
        self, album_of: AlbumFactory
    ) -> None:
        stale = album_of("aa11bb", name="Stale", tracks=0, on_disk=1)
        fresh = album_of("cc22dd", name="Fresh", tracks=0, on_disk=9)
        cache = TrackCountCache()

        # The newer refresh (generation 2) lands first.
        cache._generation = 2
        cache._refresh((fresh,))
        assert cache.get(fresh.id) == 9

        # An older refresh (generation 1), simulating one whose caller already
        # gave up and moved on, finally gets to write -- its generation is no
        # longer the newest, so the write is a no-op. Setting ``_generation``
        # back down to 1 directly (rather than via the ``+=`` production
        # path) is how this whitebox test recreates "a call that captured its
        # generation before a later one ran" without needing two real threads.
        cache._generation = 1
        cache._refresh((stale,))

        assert cache.get(fresh.id) == 9  # unchanged
        assert cache.get(stale.id) == 0  # never actually written

    def test_a_newer_generation_still_writes_normally(
        self, album_of: AlbumFactory
    ) -> None:
        album = album_of("aa11bb", tracks=0, on_disk=4)
        cache = TrackCountCache()
        cache._generation += 1
        cache._refresh((album,))
        assert cache.get(album.id) == 4

        grown = album_of("aa11bb", tracks=0, on_disk=8)
        cache._generation += 1
        cache._refresh((grown,))
        assert cache.get(album.id) == 8


class TestForTesting:
    def test_pre_populates_the_cache_synchronously(
        self, album_of: AlbumFactory
    ) -> None:
        album = album_of("aa11bb", tracks=0, on_disk=6)
        cache = TrackCountCache.for_testing((album,))
        assert cache.get(album.id) == 6

    async def test_a_later_real_refresh_can_still_write(
        self, album_of: AlbumFactory
    ) -> None:
        # Regression: for_testing wrote _written_generation to 1 via _refresh
        # but used to leave _generation at its default 0. The next real
        # serialized_refresh call would then bump _generation to only 1,
        # which fails the strict ">" gate against a _written_generation
        # already at 1 -- silently discarding a legitimate write.
        album = album_of("aa11bb", tracks=0, on_disk=3)
        cache = TrackCountCache.for_testing((album,))
        assert cache.get(album.id) == 3

        grown = album_of("aa11bb", tracks=0, on_disk=9)
        await cache.serialized_refresh((grown,))

        assert cache.get(album.id) == 9


class TestSerializedRefresh:
    """The entry point every real caller uses: off the event loop, one at a time.

    :meth:`~punt_vox.voxd.music_player.player.MusicPlayer.install` (the lux
    listener's event loop) and the single-flighted background repaint (the
    control-channel writer's event loop) can both reach this cache at once --
    an install landing while a Part-completion refresh is mid-flight. Without
    serialization, both would build ``fresh`` from their own snapshot and
    unconditionally overwrite ``_counts`` -- whichever thread happens to
    finish LAST wins, not whichever started most recently.
    """

    async def test_it_still_lands_the_live_count(self, album_of: AlbumFactory) -> None:
        album = album_of("aa11bb", tracks=0, on_disk=6)
        cache = TrackCountCache()

        await cache.serialized_refresh((album,))

        assert cache.get(album.id) == 6

    async def test_two_concurrent_callers_never_overlap(
        self, album_of: AlbumFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A slowed-down refresh body makes an unguarded overlap observable:
        # without the lock, asyncio.gather would dispatch both calls to
        # separate worker threads at once and max_in_flight would reach 2.
        guard = threading.Lock()
        in_flight = 0
        max_in_flight = 0
        real_refresh = TrackCountCache._refresh

        def _slow_refresh(self: TrackCountCache, albums: tuple[Album, ...]) -> None:
            nonlocal in_flight, max_in_flight
            with guard:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            time.sleep(0.05)
            try:
                real_refresh(self, albums)
            finally:
                with guard:
                    in_flight -= 1

        monkeypatch.setattr(TrackCountCache, "_refresh", _slow_refresh)
        album = album_of("aa11bb", tracks=0, on_disk=5)
        cache = TrackCountCache()

        await asyncio.gather(
            cache.serialized_refresh((album,)), cache.serialized_refresh((album,))
        )

        assert max_in_flight == 1


class TestSerializedRefreshTimeout:
    """A stuck disk read must not block :meth:`serialized_refresh` forever.

    ``install()`` runs inline in the lux listener's event loop, so an
    unbounded wait there freezes the whole connection's event delivery. These
    tests monkeypatch the module's timeout constant down to something the
    test suite can wait out quickly, and let the "stuck" worker thread finish
    on its own after a short, bounded sleep -- it is never left blocked
    forever, only long enough to prove it is orphaned rather than awaited.
    """

    async def test_a_stuck_refresh_times_out_and_the_caller_is_not_blocked(
        self, album_of: AlbumFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(track_count_cache_module, "_REFRESH_TIMEOUT_SECONDS", 0.05)

        def _stuck_refresh(self: TrackCountCache, albums: tuple[Album, ...]) -> None:
            time.sleep(0.3)  # much longer than the monkeypatched timeout

        monkeypatch.setattr(TrackCountCache, "_refresh", _stuck_refresh)
        album = album_of("aa11bb", tracks=0, on_disk=1)
        cache = TrackCountCache()

        # If the timeout did not bound the wait, this would hang the test.
        await asyncio.wait_for(cache.serialized_refresh((album,)), timeout=1.0)

        await asyncio.sleep(0.3)  # let the orphaned worker thread finish quietly

    async def test_a_stuck_refresh_logs_a_warning_naming_the_timeout(
        self,
        album_of: AlbumFactory,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        timeout = 0.05
        monkeypatch.setattr(
            track_count_cache_module, "_REFRESH_TIMEOUT_SECONDS", timeout
        )

        def _stuck_refresh(self: TrackCountCache, albums: tuple[Album, ...]) -> None:
            time.sleep(0.3)

        monkeypatch.setattr(TrackCountCache, "_refresh", _stuck_refresh)
        album = album_of("aa11bb", tracks=0, on_disk=1)
        cache = TrackCountCache()

        with caplog.at_level(logging.WARNING):
            await asyncio.wait_for(cache.serialized_refresh((album,)), timeout=1.0)

        expected = f"exceeded {timeout:.1f}s"
        assert any(expected in r.getMessage() for r in caplog.records)
        await asyncio.sleep(0.3)  # let the orphaned worker thread finish quietly

    async def test_a_late_orphaned_write_never_clobbers_a_newer_refresh(
        self, album_of: AlbumFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Sequence: the first refresh (generation 1) gets stuck and times out
        # from serialized_refresh's point of view, but its worker thread keeps
        # running in the background. A second, faster refresh (generation 2)
        # then lands normally. Only afterward is the orphaned first refresh
        # released to finish its write -- which must be a no-op.
        monkeypatch.setattr(track_count_cache_module, "_REFRESH_TIMEOUT_SECONDS", 0.05)
        release = threading.Event()
        done_writing = threading.Event()
        real_refresh = TrackCountCache._refresh

        def _blocking_refresh(self: TrackCountCache, albums: tuple[Album, ...]) -> None:
            # Read _generation the same way the real _refresh does -- as the
            # first thing this call does once its worker thread starts -- so
            # it captures its OWN dispatch's generation (1) even though the
            # second call bumps _generation to 2 while this one is still
            # blocked below.
            generation = self._generation
            if generation == 1:
                release.wait(timeout=2.0)
            real_refresh(self, albums)
            if generation == 1:
                done_writing.set()

        monkeypatch.setattr(TrackCountCache, "_refresh", _blocking_refresh)
        stale = album_of("aa11bb", name="Stale", tracks=0, on_disk=1)
        fresh = album_of("cc22dd", name="Fresh", tracks=0, on_disk=9)
        cache = TrackCountCache()

        # Generation 1: times out from the caller's side; the worker thread is
        # still blocked on `release`.
        await asyncio.wait_for(cache.serialized_refresh((stale,)), timeout=1.0)
        assert cache.get(stale.id) == 0  # not yet written -- still blocked

        # Generation 2: the async lock was freed by the timeout, so this lands
        # normally while the orphan from generation 1 is still parked.
        await cache.serialized_refresh((fresh,))
        assert cache.get(fresh.id) == 9

        # Now let the orphaned generation-1 write finally happen.
        release.set()
        await asyncio.to_thread(done_writing.wait, 2.0)

        assert cache.get(fresh.id) == 9  # unchanged by the late orphaned write
        assert cache.get(stale.id) == 0  # the orphan's write was a no-op

    async def test_a_genuine_disk_timeout_propagates_rather_than_being_swallowed(
        self, album_of: AlbumFactory
    ) -> None:
        # A real disk-level timeout (e.g. errno.ETIMEDOUT from a hung NFS
        # mount) raises TimeoutError too -- a built-in OSError subclass --
        # and this one completes well within the default
        # _REFRESH_TIMEOUT_SECONDS. It must not be mistaken for wait_for's
        # own deadline and swallowed as "gave up waiting".
        album = album_of("aa11bb", fails_with=TimeoutError("ETIMEDOUT"))
        with pytest.raises(TimeoutError, match="ETIMEDOUT"):
            await TrackCountCache().serialized_refresh((album,))
