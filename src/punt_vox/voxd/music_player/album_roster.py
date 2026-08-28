"""``AlbumRoster`` -- the catalog as display rows, each with its live track count.

This is the one seam where the scene's per-album data leaves the store, and that
is the point. An album's Parts are a *disk read*: the background fill grows the
on-disk manifest long after the catalog registers the album, so the creation-time
snapshot reports zero for the album's whole life. Counting them live is therefore
mandatory -- but counting them *inside* the projection would put a stat, a read,
and a JSON parse per album into a render that documents itself as I/O-free, on the
control-channel single-writer thread, with a raise path through the middle of it.

Reading here instead, once per projection at the player's own seam, keeps
:class:`~punt_vox.voxd.music_player.scene.AlbumListScene` the pure function of its
inputs it claims to be, and keeps a deleted album's ``LookupError`` out of the
render path entirely.

An album the store no longer holds is dropped from *both* the album tuple and the
display tuple, so the table, the names map, and the player view all see one
coherent snapshot. Only ``LookupError`` -- the store's documented "this album was
deleted" contract -- drops an album. Every other fault propagates: a transient
``OSError`` silently vanishing an album from the widget would be a lie the user
has no way to diagnose.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd.music_player.album_display import AlbumDisplay

if TYPE_CHECKING:
    from punt_vox.voxd.programs.catalog import Album

__all__ = ["AlbumRoster"]

logger = logging.getLogger(__name__)


@final
class AlbumRoster:
    """The readable catalog paired with each album's live ready-Part count."""

    __slots__ = ("_albums", "_displays")
    _displays: tuple[AlbumDisplay, ...]
    # Derived once here rather than per access: the scene reads it three times
    # per render (the table, the names map, and the player view), and a property
    # that rebuilds a tuple each time is a method wearing an attribute's clothes.
    _albums: tuple[Album, ...]

    def __new__(cls, displays: tuple[AlbumDisplay, ...]) -> Self:
        self = super().__new__(cls)
        self._displays = displays
        self._albums = tuple(display.album for display in displays)
        return self

    @classmethod
    def read(cls, albums: tuple[Album, ...]) -> Self:
        """Pair each album with its live track count, dropping any since deleted."""
        return cls(tuple(cls._readable(albums)))

    @property
    def albums(self) -> tuple[Album, ...]:
        """Return the albums the store still holds, in catalog order."""
        return self._albums

    @property
    def displays(self) -> tuple[AlbumDisplay, ...]:
        """Return one display row per readable album, in the same order."""
        return self._displays

    @staticmethod
    def _readable(albums: tuple[Album, ...]) -> list[AlbumDisplay]:
        """Return a display for each album still on disk; drop the deleted ones."""
        readable: list[AlbumDisplay] = []
        for album in albums:
            try:
                readable.append(AlbumDisplay.read(album))
            except LookupError:
                # The store's documented delete contract. Anything else -- an
                # OSError from a permission blip or a descriptor exhaustion --
                # propagates rather than vanishing an album the user still has.
                logger.debug(
                    "album %s is no longer on disk; dropping its row", album.id
                )
        return readable
