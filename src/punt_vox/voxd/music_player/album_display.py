"""``AlbumDisplay`` -- one album's per-album display cells for the ``vox.music`` scene.

The scene renders an album's non-name cells the same way wherever it appears: its
genre and its track count, plus the album's opaque id (never shown; the resolution
target). Those per-album derivations live here, on the object that owns one album's
display, so no free function reaches into ``Album``'s manifest to recompute them
(PY-OO-7).

The album's *friendly name* is deliberately not here: a name must be unique across
the whole catalog to serve as a resolvable key cell, so it -- and the inverse
name-to-album resolution -- lives on :class:`AlbumNames`, which is built once from
the full catalog. ``AlbumDisplay`` needs only the single album; ``AlbumNames``
needs them all.

The track count is *held*, not fetched. ``genre`` and ``id`` read durable metadata
that is fixed at creation, but an album's Parts grow on disk as the background
fill lands, so the count has to be read live -- and a live read belongs at the
player's seam, not inside a render. :meth:`AlbumDisplay.read` is that read;
:class:`~punt_vox.voxd.music_player.album_roster.AlbumRoster` is where it happens,
once per projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from punt_vox.voxd.programs.catalog import Album

__all__ = ["AlbumDisplay"]


@final
@dataclass(frozen=True, slots=True)
class AlbumDisplay:
    """The per-album display projection: the genre and track-count cells, and the id."""

    album: Album
    tracks: int  # ready Parts as of this projection -- never the frozen snapshot

    @classmethod
    def read(cls, album: Album) -> Self:
        """Pair ``album`` with its ready-Part count, read live from the store.

        Raises ``LookupError`` when the store no longer holds the album; the
        roster catches that and drops the row.
        """
        return cls(album, len(album.ready_parts()))

    @property
    def id(self) -> str:
        """Return the album's opaque id string (never shown; the resolution target)."""
        return self.album.id.value

    @property
    def genre(self) -> str:
        """Return the album's style tag -- the Genre column cell."""
        return self.album.manifest.tags.style
