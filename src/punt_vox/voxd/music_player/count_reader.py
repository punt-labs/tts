"""``CountReader`` -- how a ready-track count comes off disk.

The third of the three jobs :class:`~punt_vox.voxd.music_player.
track_count_cache.TrackCountCache` used to do alone: *when* to read (the
cache: a dispatch, a lock, a deadline), *whether* to keep the answer
(:class:`~punt_vox.voxd.music_player.count_store.CountStore`: generation
ordering), and -- here -- the read itself.

Worth its own name because it is the only blocking call in the whole path.
Everything around it exists to keep it off an event loop and off the
control-channel writer; keeping it in one obvious place is what makes that
claim checkable rather than something a reader has to take on trust.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd.music_player.album_display import AlbumDisplay

if TYPE_CHECKING:
    from punt_vox.voxd.programs.album_id import AlbumId
    from punt_vox.voxd.programs.catalog import Album

__all__ = ["CountReader"]

logger = logging.getLogger(__name__)


@final
class CountReader:
    """Reads each album's live ready-track count from its on-disk manifest."""

    __slots__ = ()

    def __new__(cls) -> Self:
        return super().__new__(cls)

    def read(self, albums: tuple[Album, ...]) -> dict[AlbumId, int]:
        """Return a fresh count per album; blocking, one disk read each.

        An album the store no longer holds is dropped rather than counted:
        ``LookupError`` is the catalog's documented "this was deleted"
        contract. Every other fault propagates -- a permission blip or
        descriptor exhaustion must surface, not silently freeze one album's
        count at its last-known value with nothing to say why.

        Dropping a deleted album here is not what keeps it off the screen --
        the catalog is. Deletion removes an album from the catalog
        synchronously in the same call that removes it from disk
        (:meth:`~punt_vox.voxd.programs.library.Library.remove`), well
        before any render sees it again; this only keeps a refresh that
        raced that removal from failing over it.
        """
        counts: dict[AlbumId, int] = {}
        for album in albums:
            try:
                counts[album.id] = AlbumDisplay.read(album).tracks
            except LookupError:
                logger.debug(
                    "album %s is no longer on disk; dropping its cached count",
                    album.id,
                )
        return counts
