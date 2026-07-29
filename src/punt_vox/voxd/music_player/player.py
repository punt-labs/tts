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

from punt_vox.voxd.music_player.player_view import PlayerView
from punt_vox.voxd.music_player.scene import AlbumListScene

if TYPE_CHECKING:
    from punt_vox.voxd.music_player.ports import PlayerService, ScenePublisher

__all__ = ["MusicPlayer"]


@final
class MusicPlayer:
    """Re-project the ``vox.music`` scene on every playback or catalog change."""

    __slots__ = ("_publisher", "_service")
    _service: PlayerService
    _publisher: ScenePublisher

    def __new__(cls, service: PlayerService, publisher: ScenePublisher) -> Self:
        self = super().__new__(cls)
        self._service = service
        self._publisher = publisher
        return self

    def notify_changed(self) -> None:
        """Re-project the scene from fresh status + catalog and submit it.

        Non-blocking: builds the scene synchronously and hands it to the mailbox;
        the blocking push runs on the publisher's task.
        """
        albums = self._service.catalog_albums()
        view = PlayerView.from_status(self._service.status(), albums)
        self._publisher.submit(AlbumListScene(albums, view).render_request())
