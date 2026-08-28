"""``MusicPlayer`` -- project voxd's active source onto the lux music scene.

The player is the daemon's :class:`ChangeListener`: on each notification it reads
the fresh status and catalog, builds the :class:`PlayerView` and the
:class:`AlbumListScene`, and hands the rendered scene to the publisher's mailbox.
Everything it does is fast, synchronous, non-blocking work -- the blocking REST
push happens on the publisher's own task, so the control-channel single-writer
that fires the notification is never held up.

Every projection here is the same tree; what differs is the *intent* the player
attaches to it. :meth:`MusicPlayer.notify_changed` and the failure presenters all
refresh, so a window the user has parked stays parked. :meth:`MusicPlayer.install`
is the one verb that shows -- and it is reached only from the Music menu click and
the hub handshake, the two moments that genuinely mean "put this in front of me".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd.music_player.album_roster import AlbumRoster
from punt_vox.voxd.music_player.playback_notice import PlaybackNotice
from punt_vox.voxd.music_player.player_view import PlayerView
from punt_vox.voxd.music_player.scene import AlbumListScene

if TYPE_CHECKING:
    from punt_lux import RenderRequest

    from punt_vox.voxd.music_player.ports import PlayerService, ScenePublisher
    from punt_vox.voxd.programs.album_id import AlbumId
    from punt_vox.voxd.programs.catalog import Album

__all__ = ["MusicPlayer"]


@final
class MusicPlayer:
    """Re-project the ``vox.music`` scene on every playback or catalog change.

    As the daemon's :class:`ChangeListener` it repaints silently on every state
    change; as the receive leg's :class:`ScenePresenter` it also repaints with a
    transient warning when a clicked Play or Stop could not be applied, and
    installs the scene outright on the two triggers that mean the user asked to
    see the window.
    """

    __slots__ = ("_publisher", "_service")
    _service: PlayerService
    _publisher: ScenePublisher

    def __new__(cls, service: PlayerService, publisher: ScenePublisher) -> Self:
        self = super().__new__(cls)
        self._service = service
        self._publisher = publisher
        return self

    def notify_changed(self) -> None:
        """Re-project the scene from fresh status + catalog and submit it (silent).

        Non-blocking: builds the scene synchronously and hands it to the mailbox;
        the blocking push runs on the publisher's task. Carrying the silent notice
        clears any warning a prior failed click had raised.
        """
        self._submit(self._service.catalog_albums(), PlaybackNotice.silent())

    def install(self) -> None:
        """Re-project the scene and install it, raising its frame.

        The one difference from :meth:`notify_changed` is intent, not content: a
        track change is a refresh of a window the user has already placed, while a
        menu click or a fresh hub connection is a request to see the window.
        """
        albums = self._service.catalog_albums()
        self._publisher.reinstall(self._render(albums, PlaybackNotice.silent()))

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
        """Project from fresh status, ``albums`` and ``notice``; submit a refresh."""
        self._publisher.submit(self._render(albums, notice))

    def _render(
        self, albums: tuple[Album, ...], notice: PlaybackNotice
    ) -> RenderRequest:
        """Return the scene projected from fresh status, ``albums`` and ``notice``.

        The roster read is the one live store read on this path, and it happens
        here rather than in the projection: the scene stays a pure function of
        what it is handed, and every cell of one render sees one coherent
        snapshot of the catalog.
        """
        roster = AlbumRoster.read(albums)
        view = PlayerView.from_status(self._service.status(), roster.albums)
        return AlbumListScene(roster, view, notice).render_request()
