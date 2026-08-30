"""``MusicPlayer`` -- project voxd's active source onto the lux music scene.

The player is the daemon's :class:`ChangeListener`: on each notification it reads
the fresh status and catalog, builds the :class:`PlayerView` and the
:class:`AlbumListScene`, and hands the rendered scene to the publisher's mailbox.
:meth:`notify_changed` and the failure presenters must return at once -- they run
on the control-channel single-writer, which every playback mutation is
serialized behind -- so the render they build reads each album's track count
from :class:`~punt_vox.voxd.music_player.track_count_cache.TrackCountCache`
rather than the disk: a cache lookup, never a stat/open/read/JSON-parse per
album. :meth:`install` runs on the lux listener's event loop instead (the Music
menu click, or a hub handshake), and it can afford to await a fresh read --
:meth:`~punt_vox.voxd.music_player.track_count_cache.TrackCountCache.
serialized_refresh` keeps that disk read off the loop the session's lease
keepalive shares, matching the pattern
:class:`~punt_vox.panel.panel_runner.PanelRunner` already uses for its own
prefetch.

Every projection here is the same tree; what differs is the *intent* the player
attaches to it. :meth:`MusicPlayer.notify_changed` and the failure presenters all
refresh, so a window the user has parked stays parked. :meth:`MusicPlayer.install`
is the one verb that shows -- and it is reached only from the Music menu click and
the hub handshake, the two moments that genuinely mean "put this in front of me".
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
        """Refresh the live track counts, re-project the scene, and install it.

        The one difference from :meth:`notify_changed` is intent, not content: a
        track change is a refresh of a window the user has already placed, while
        a menu click or a fresh hub connection is a request to see the window --
        and since this call already runs on the lux listener's event loop (never
        the control-channel writer), it can afford to await a fresh disk read via
        :meth:`~punt_vox.voxd.music_player.track_count_cache.TrackCountCache.
        serialized_refresh` before it shows, rather than trust a background
        repaint to have already landed one.

        A refresh failure is logged and swallowed by :meth:`_try_refresh_cache`,
        never left to propagate: the outer lux boundary that calls this only
        logs an unhandled exception, so a raise here would mean the menu click
        that asked to see the window produces nothing visible at all.
        Installing with whatever counts the cache already holds -- stale, but
        on screen -- beats that outcome. ``_latest_notice`` is set here exactly
        like every path through :meth:`_submit`, so a background refresh that
        resolves after this call never resurrects a warning this call just
        cleared.
        """
        albums = self._service.catalog_albums()
        await self._try_refresh_cache(albums)
        notice = PlaybackNotice.silent()
        self._latest_notice = notice
        self._publisher.reinstall(self._render(albums, notice))

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
        """Project from fresh status, ``albums`` and ``notice``; submit a refresh.

        Also schedules a best-effort background cache refresh: the render this
        call submits reads whatever the cache already holds, which may be a
        render or two behind the true disk state until that refresh lands and
        resubmits.
        """
        self._latest_notice = notice
        self._publisher.submit(self._render(albums, notice))
        self._schedule_track_count_refresh(albums)

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

    def _schedule_track_count_refresh(self, albums: tuple[Album, ...]) -> None:
        """Best-effort: warm the cache off the hot path, then resubmit if it changes.

        Single-flighted via :attr:`_refresher`: a burst of state changes (one
        per completed Part) that arrives while a refresh is already in flight
        is dropped outright, not merged with it -- safe here only because
        :meth:`_refresh_track_counts` re-reads the live catalog and
        :attr:`_latest_notice` fresh at execution time rather than closing
        over the ``albums`` snapshot captured when this call was scheduled, so
        the run already in flight still picks up whatever state a dropped
        call would have carried.
        """
        self._refresher.schedule(lambda: self._refresh_track_counts(albums))

    async def _refresh_track_counts(self, albums: tuple[Album, ...]) -> None:
        """Refresh the cache from disk, off the control-channel writer entirely.

        A failure here is logged and dropped by :meth:`_try_refresh_cache`: a
        stale track count is a display nit, never a reason to take down the
        write path that fired this refresh. On success, resubmits with
        whatever notice is current at THIS moment -- never the one captured
        when the refresh was scheduled -- so a warning raised, or cleared,
        while the refresh was in flight is never clobbered or resurrected by a
        repaint that started before it happened.
        """
        if not await self._try_refresh_cache(albums):
            return
        # Fresh, not the snapshot the refresh was scheduled with: the catalog
        # itself may have gained or lost an album while the disk read ran.
        fresh_albums = self._service.catalog_albums()
        self._publisher.submit(self._render(fresh_albums, self._latest_notice))

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
