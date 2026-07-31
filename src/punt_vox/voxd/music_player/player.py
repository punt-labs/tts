"""``MusicPlayer`` -- project voxd's active source onto the lux music scene.

The player is the daemon's :class:`ChangeListener`: on each notification it reads
the fresh status and catalog, builds the :class:`PlayerView` and the
:class:`AlbumListScene`, and hands the rendered scene to the publisher's mailbox.
Everything it does is fast, synchronous, non-blocking work -- the blocking REST
push happens on the publisher's own task, so the control-channel single-writer
that fires the notification is never held up.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd.music_player.playback_notice import PlaybackNotice
from punt_vox.voxd.music_player.player_view import PlayerView
from punt_vox.voxd.music_player.scene import AlbumListScene

if TYPE_CHECKING:
    from punt_vox.voxd.music_player.ports import PlayerService, ScenePublisher
    from punt_vox.voxd.programs.album_id import AlbumId
    from punt_vox.voxd.programs.catalog import Album

__all__ = ["MusicPlayer"]


@final
class MusicPlayer:
    """Re-project the ``vox.music`` scene on every playback or catalog change.

    As the daemon's :class:`ChangeListener` it repaints silently on every state
    change; as the receive leg's :class:`ScenePresenter` it also repaints with a
    transient warning when a clicked Play or Stop could not be applied.
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

    def _submit(self, albums: tuple[Album, ...], notice: PlaybackNotice) -> None:
        """Project the scene from fresh status, ``albums`` and ``notice``; submit it."""
        view = PlayerView.from_status(self._service.status(), albums)
        self._publisher.submit(AlbumListScene(albums, view, notice).render_request())
