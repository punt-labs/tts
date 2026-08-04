"""``AlbumNames`` -- the catalogue-wide friendly-name map and its inverse.

One album's display cells live on :class:`AlbumDisplay`, but a *friendly name*
is not a per-album property: it must be unique across the whole catalog so a
clicked row resolves back to exactly one album. ``AlbumNames`` builds that map
once from the full catalog: each album's title-cased, timestamp-free
:meth:`AlbumTags.display_title` is its base name, and when two albums share a
base -- two pools of the same ``(style, vibe)`` minted minutes apart -- every
colliding album is suffixed with its short id (``Synthwave (a1b2c3)``) so each
row's key cell stays catalogue-unique. Dropping the timestamp is why they
collide; the id suffix is why click-to-play still resolves the one the user
clicked, never its twin.

The map is invertible: :meth:`resolve` maps a clicked row's key cell -- the
album's plain friendly name -- back to its :class:`Album`. The name cell carries
no now-playing marker (which would change its sort key and its identity as the
click key); the now-playing album is shown in the now-playing block, not in the
table. Both the scene that renders the cells and the receive leg that resolves a
click build ``AlbumNames`` from the same catalog snapshot, so the names they
produce and consume agree.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from punt_vox.voxd.programs.album_id import AlbumId
    from punt_vox.voxd.programs.catalog import Album

__all__ = ["AlbumNames"]


@final
class AlbumNames:
    """The catalogue-wide album <-> friendly-name map, built once and invertible."""

    __slots__ = ("_by_cell", "_by_id")
    _by_id: dict[AlbumId, str]  # album id -> its unique friendly name
    _by_cell: dict[str, Album]  # friendly name -> album

    def __new__(cls, albums: tuple[Album, ...]) -> Self:
        self = super().__new__(cls)
        self._by_id = cls._assign(albums)
        self._by_cell = {self._by_id[album.id]: album for album in albums}
        return self

    @staticmethod
    def _assign(albums: tuple[Album, ...]) -> dict[AlbumId, str]:
        """Return each album's unique friendly name, id-suffixing every collision.

        A base name shared by two or more albums is not unique, so *every* album
        carrying it is suffixed with its short id -- an order-independent rule, so
        an album's name depends only on whether a twin exists, not on catalog
        position. A base owned by one album is left clean.
        """
        bases = {album.id: album.manifest.tags.display_title() for album in albums}
        counts = Counter(bases.values())
        return {
            album_id: base if counts[base] == 1 else f"{base} ({album_id.value})"
            for album_id, base in bases.items()
        }

    def friendly(self, album: Album) -> str:
        """Return the album's unique friendly name (its plain name-cell text)."""
        return self._by_id[album.id]

    def resolve(self, anchor: str) -> Album:
        """Return the album whose name cell the clicked ``anchor`` names, or raise.

        ``anchor`` is the ``key_column`` cell of the selected row -- an album's
        plain friendly name. An anchor that names no album is a stale or unknown
        click and raises (PY-EH-8), so the receive boundary drops it rather than
        playing the wrong album.
        """
        album = self._by_cell.get(anchor)
        if album is None:
            msg = f"music.play anchor {anchor!r} names no catalogued album"
            raise ValueError(msg)
        return album
