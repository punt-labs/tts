"""``MusicPlayer`` -- project voxd's active source onto the lux music scene.

The player is the daemon's :class:`ChangeListener`: on each notification it reads
the fresh status and catalog, builds the :class:`PlayerView` and the
:class:`AlbumListScene`, and hands the rendered scene to the publisher's mailbox.
Every entry point -- :meth:`notify_changed`, the failure presenters, and
:meth:`install` alike -- must return at once, since any of them can run on the
lux listener's event loop (a menu click, a hub handshake) as readily as the
control-channel single-writer. So every render reads each album's track count
from :class:`~punt_vox.voxd.music_player.track_count_cache.TrackCountCache`
rather than the disk, and the live refresh that keeps the cache honest always
runs as a best-effort background task via :meth:`_schedule_track_count_refresh`,
never awaited inline: paint from whatever is already known, refresh live in the
background, and resubmit once that refresh lands.

Every projection is the same tree; only the *intent* differs. :meth:`notify_changed`
and the failure presenters refresh, so a parked window stays parked;
:meth:`install` is the one verb that shows, reached only from the Music menu
click and the hub handshake.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd.music_player.album_roster import AlbumRoster
from punt_vox.voxd.music_player.playback_notice import PlaybackNotice
from punt_vox.voxd.music_player.player_view import PlayerView
from punt_vox.voxd.music_player.scene import AlbumListScene
from punt_vox.voxd.music_player.single_flight import SingleFlightRefresh
from punt_vox.voxd.music_player.track_count_cache import TrackCountCache

if TYPE_CHECKING:
    from punt_lux import RenderRequest

    from punt_vox.voxd.music_player.ports import PlayerService, ScenePublisher
    from punt_vox.voxd.programs.album_id import AlbumId
    from punt_vox.voxd.programs.catalog import Album

__all__ = ["MusicPlayer"]

logger = logging.getLogger(__name__)


@final
class MusicPlayer:
    """Re-project the ``vox.music`` scene on every playback or catalog change.

    As the daemon's :class:`ChangeListener` it repaints silently on every state
    change; as the receive leg's :class:`ScenePresenter` it also repaints with a
    transient warning when a clicked Play or Stop could not be applied, and
    installs the scene outright on the two triggers that mean the user asked to
    see the window.
    """

    __slots__ = ("_cache", "_latest_notice", "_publisher", "_refresher", "_service")
    _service: PlayerService
    _publisher: ScenePublisher
    _cache: TrackCountCache
    # A background cache refresh is single-flighted: a burst of state changes --
    # one per completed Part -- must not queue an unbounded pile of overlapping
    # disk reads.
    _refresher: SingleFlightRefresh
    # The notice a background refresh's resubmit must carry -- read fresh at
    # resubmit time (not the notice captured when the refresh was scheduled) so
    # a warning surfaced *after* scheduling is never clobbered by a refresh that
    # started before it, and a warning already cleared is never resurrected.
    _latest_notice: PlaybackNotice

    def __new__(cls, service: PlayerService, publisher: ScenePublisher) -> Self:
        self = super().__new__(cls)
        self._service = service
        self._publisher = publisher
        self._cache = TrackCountCache()
        self._refresher = SingleFlightRefresh()
        self._latest_notice = PlaybackNotice.silent()
        return self

    def notify_changed(self) -> None:
        """Re-project the scene from fresh status + catalog and submit it (silent).

        Non-blocking: builds the scene synchronously from the cached track
        counts and hands it to the mailbox; the blocking push runs on the
        publisher's task, and the cache's own disk read runs on its own
        best-effort background task (never awaited here). Carrying the silent
        notice clears any warning a prior failed click had raised.
        """
        self._submit(self._service.catalog_albums(), PlaybackNotice.silent())

    async def install(self) -> None:
        """Project the scene from cached track counts and install it at once.

        Differs from :meth:`notify_changed` in intent, not content: a menu
        click or fresh hub connection means "show this", so it installs
        (raises the frame) rather than merely refreshing. Async only for
        :class:`~punt_vox.voxd.music_player.presenter_ports.ScenePresenter`;
        nothing here awaits -- this runs on the event loop that holds the hub
        connection's session lease, so it must never await the bounded-but-up-
        to-5s :meth:`~punt_vox.voxd.music_player.track_count_cache.
        TrackCountCache.serialized_refresh` before showing anything, as it
        used to. Instead it installs from whatever :attr:`_cache` already
        holds and hands the live refresh to :meth:`_schedule_track_count_
        refresh`, the same background path :meth:`notify_changed` uses.
        """
        albums = self._service.catalog_albums()
        notice = PlaybackNotice.silent()
        self._latest_notice = notice
        self._publisher.reinstall(self._render(albums, notice))
        self._schedule_track_count_refresh()

    def present_play_failure(self, album: AlbumId) -> None:
        """Re-project the scene warning that ``album`` could not play (transient).

        A play that raised changed no daemon state, so the view is rebuilt from the
        unchanged status -- an idle play stays idle, preserving I2 -- and the warning
        rides beside it as the notice. The next legitimate change clears it.
        """
        albums = self._service.catalog_albums()
        self._submit(albums, PlaybackNotice.play_failed(album, albums))

    def present_stop_failure(self) -> None:
        """Re-project the scene warning that the stop could not apply (transient)."""
        self._submit(self._service.catalog_albums(), PlaybackNotice.stop_failed())

    def present_resolve_failure(self, anchor: str) -> None:
        """Re-project with a warning that ``anchor`` names no album (transient).

        Parallel to :meth:`present_play_failure`, but nothing resolved: the
        anchor was well-formed yet the catalog no longer holds it. The warning
        names the clicked anchor text, not an id, and the next legitimate change
        clears it.
        """
        self._submit(
            self._service.catalog_albums(), PlaybackNotice.resolve_failed(anchor)
        )

    def present_transport_failure(self) -> None:
        """Re-project the scene silently after a refused transport control.

        A transport command (prev/next/pause/resume) names no album, so there is no
        per-album warning to raise; a plain re-push from the unchanged status keeps
        the scene truthful without a spurious notice.
        """
        self._submit(self._service.catalog_albums(), PlaybackNotice.silent())

    def _submit(self, albums: tuple[Album, ...], notice: PlaybackNotice) -> None:
        """Project from ``albums`` and ``notice``; submit a refresh.

        Paints immediately from whatever :attr:`_cache` already holds -- a
        dict lookup, never a disk read, so this never blocks its caller -- and
        schedules the same background refresh :meth:`install` also triggers,
        converging once it lands. :meth:`install` does not call this: it
        installs (raises the frame) rather than merely refreshing, so it
        repeats this shape against ``reinstall`` instead of ``submit``.
        """
        self._latest_notice = notice
        self._publisher.submit(self._render(albums, notice))
        self._schedule_track_count_refresh()

    def _render(
        self, albums: tuple[Album, ...], notice: PlaybackNotice
    ) -> RenderRequest:
        """Return the scene projected from fresh status, ``albums`` and ``notice``.

        Track counts come from :attr:`_cache` -- a dict lookup, never a disk
        read -- so this stays safe to call from the control-channel single-writer
        as well as the lux listener's event loop.
        """
        roster = AlbumRoster.from_cache(albums, self._cache)
        view = PlayerView.from_status(self._service.status(), roster.albums)
        return AlbumListScene(roster, view, notice).render_request()

    def _schedule_track_count_refresh(self) -> None:
        """Best-effort: warm the cache off the hot path, then resubmit if it changes.

        Single-flighted via :attr:`_refresher`: a burst of state changes (one
        per completed Part) arriving while a refresh is in flight is dropped
        outright, not merged -- safe only because :meth:`_refresh_track_counts`
        re-reads the live catalog fresh at execution time, so the run already
        in flight still picks up a newly joined album a dropped call carried.
        """
        self._refresher.schedule(self._refresh_track_counts)

    async def _refresh_track_counts(self) -> None:
        """Refresh the cache from disk, off the control-channel writer entirely.

        Reads the live catalog fresh, at execution time, and reuses that SAME
        read for both halves of the operation: the refresh itself
        (:meth:`_try_refresh_cache`) and the resubmit below. Splitting those
        two reads is exactly the bug this closes: an album joining the catalog
        between scheduling and this run's disk read would be excluded from
        :class:`~punt_vox.voxd.music_player.track_count_cache.TrackCountCache`'s
        wholesale-replaced ``_counts`` dict, then rendered at 0 tracks in the
        resubmit -- stuck there, since SingleFlightRefresh had already dropped
        the second ``schedule`` call that would otherwise have picked it up.

        A failure here is logged and dropped by :meth:`_try_refresh_cache`. On
        success, resubmits with whatever notice is current at THIS moment --
        not the one captured at schedule time -- so a warning is never lost.
        """
        albums = self._service.catalog_albums()
        if not await self._try_refresh_cache(albums):
            return
        self._publisher.submit(self._render(albums, self._latest_notice))

    async def _try_refresh_cache(self, albums: tuple[Album, ...]) -> bool:
        """Refresh :attr:`_cache`; log and report failure rather than raise.

        Both callers -- :meth:`install` and :meth:`_refresh_track_counts` --
        treat a refresh fault as non-fatal: a stale count is a display nit,
        never a reason to sink a menu click or take down the write path that
        fired a background refresh. The log line names how many albums this
        refresh covered, so a persistently failing catalog is diagnosable from
        the log rather than an unqualified line repeating forever.

        Narrowed to ``(OSError, ValueError)`` on purpose, not a bare
        ``Exception``: both name a store-side data condition rather than a
        programmer bug -- ``OSError`` covers disk pressure, descriptor
        exhaustion, and a genuine read-level timeout; ``ValueError`` covers
        ``AlbumManifest.from_json`` rejecting a corrupt on-disk manifest
        record, which the store's own ``open()`` raises uncaught. A real bug
        (``AttributeError``, ``TypeError``) still propagates rather than being
        swallowed under the same log line and severity as store corruption.
        """
        try:
            await self._cache.serialized_refresh(albums)
        except (OSError, ValueError):
            logger.exception(
                "music: track-count cache refresh failed for %d albums", len(albums)
            )
            return False
        return True
