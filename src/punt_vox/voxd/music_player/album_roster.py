"""``AlbumRoster`` -- the catalog as display rows, each with its live track count.

This is the one seam where the scene consumes the per-album track count, and
the point of it is that it does NOT touch disk. An album's Parts are a *disk
read* -- the background fill grows the on-disk manifest long after the catalog
registers the album, so the creation-time snapshot reports zero for the
album's whole life -- but reading them live, inline, on every projection would
put a stat, a read, and a JSON parse per album into a render that runs on the
control-channel single-writer and on the lux listener's event loop, both places
a blocking disk read must never run.

:class:`~punt_vox.voxd.music_player.track_count_cache.TrackCountCache` is where
the disk read actually happens now, off the hot path via ``asyncio.to_thread``.
:meth:`from_cache` builds this roster from that cache alone -- a dict lookup per
album, never disk -- so :class:`~punt_vox.voxd.music_player.scene.AlbumListScene`
stays the pure function of its inputs it claims to be.

An album the cache has never refreshed reads as a zero track count rather than
being dropped: unlike the disk read this replaced, a cache lookup cannot
distinguish "deleted" from "not yet refreshed," so it is the catalog -- not this
roster -- that decides which albums exist at all. That is not a loss of
coherence: an album's removal already drops it from the catalog synchronously,
in the very call that deletes its directory
(:meth:`~punt_vox.voxd.programs.library.Library.remove`), so by the time any
render runs again the deleted album is simply absent from ``albums`` -- there
is no window where a live disk check on THIS roster's read path was ever the
thing keeping it out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd.music_player.album_display import AlbumDisplay

if TYPE_CHECKING:
    from punt_vox.voxd.music_player.track_count_cache import TrackCountCache
    from punt_vox.voxd.programs.catalog import Album

__all__ = ["AlbumRoster"]


@final
class AlbumRoster:
    """The readable catalog paired with each album's cached ready-Part count."""

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
    def from_cache(cls, albums: tuple[Album, ...], cache: TrackCountCache) -> Self:
        """Pair each album with its cached track count -- no disk touched here."""
        return cls(tuple(AlbumDisplay(album, cache.get(album.id)) for album in albums))

    @property
    def albums(self) -> tuple[Album, ...]:
        """Return the albums this roster was built from, in catalog order."""
        return self._albums

    @property
    def displays(self) -> tuple[AlbumDisplay, ...]:
        """Return one display row per album, in the same order."""
        return self._displays
