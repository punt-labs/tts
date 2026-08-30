"""``TrackCountCache`` -- the one place left that touches disk for a track count.

Each album's ready-Part count is a *disk read* (a stat, an open, a read, and a
JSON parse of its manifest -- see :meth:`~punt_vox.voxd.programs.catalog.Album.
ready_parts`), because the background fill grows the on-disk manifest long after
an album is minted. :class:`~punt_vox.voxd.music_player.player.MusicPlayer`
used to re-run that read, per album, inline inside every projection -- on the
control-channel single-writer for a state-change repaint, and on the lux
listener's event loop for a menu click or a hub handshake. Both are places a
blocking disk read must never run: the writer serializes every playback
mutation behind it, and the listener holds the session's lease keepalive.

This cache is the fix: :meth:`refresh` is the one place that still does the
disk read, and it is always run off the hot path via ``asyncio.to_thread`` (see
:meth:`~punt_vox.voxd.music_player.player.MusicPlayer.install` and
:meth:`~punt_vox.voxd.music_player.player.MusicPlayer._refresh_track_counts`).
Every render reads :meth:`get` instead -- an in-memory dict lookup, never disk.

An album this cache has never seen reads as zero, matching a genuinely fresh
album before its first Part lands, so an unrefreshed row looks identical to a
real empty one rather than some other placeholder (PY-TS-14: the *absence* of a
cached count and a *genuine* zero count are meant to render the same way; there
is no third state a caller could distinguish them by, nor would one be useful
here). An album the store no longer holds is simply dropped from a refresh
(:meth:`refresh` catches its ``LookupError`` and excludes it) -- the catalog
itself, not this cache, is what stops a deleted album from being rendered at
all, since deletion removes it from the catalog synchronously in the same call
that removes it from disk (:meth:`~punt_vox.voxd.programs.library.Library.
remove`), well before any render sees it again.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd.music_player.album_display import AlbumDisplay

if TYPE_CHECKING:
    from punt_vox.voxd.programs.album_id import AlbumId
    from punt_vox.voxd.programs.catalog import Album

__all__ = ["TrackCountCache"]

logger = logging.getLogger(__name__)


@final
class TrackCountCache:
    """The last-known ready-track count per album, refreshed off the hot path."""

    __slots__ = ("_counts",)
    _counts: dict[AlbumId, int]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._counts = {}
        return self

    def get(self, album_id: AlbumId) -> int:
        """Return the last-known ready-track count for ``album_id``, or zero."""
        return self._counts.get(album_id, 0)

    def refresh(self, albums: tuple[Album, ...]) -> None:
        """Re-read every album's live count from disk; the one blocking call here.

        Callers MUST run this via ``asyncio.to_thread`` -- never inline on the
        control-channel single-writer or the lux listener's event loop. Only
        ``LookupError`` (the store's documented "this album was deleted"
        contract) drops an album from the refreshed set; any other fault -- a
        transient ``OSError`` from a permission blip or a descriptor exhaustion
        -- propagates rather than silently freezing that album's count at its
        last-known value.
        """
        fresh: dict[AlbumId, int] = {}
        for album in albums:
            try:
                fresh[album.id] = AlbumDisplay.read(album).tracks
            except LookupError:
                logger.debug(
                    "album %s is no longer on disk; dropping its cached count",
                    album.id,
                )
        self._counts = fresh
