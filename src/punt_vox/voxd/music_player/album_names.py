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
plain friendly name, or the ``▶``-marked form the playing row carries -- back to
its :class:`Album`. Both the scene that renders the cells and the receive leg
that resolves a click build ``AlbumNames`` from the same catalog snapshot, so
the names they produce and consume agree.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from punt_vox.voxd.music_player.player_view import PlayerView
    from punt_vox.voxd.programs.album_id import AlbumId
    from punt_vox.voxd.programs.catalog import Album

__all__ = ["AlbumNames"]

_MARKER = "▶ "  # the now-playing cue prefixed to the playing album's name cell


@final
class AlbumNames:
    """The catalogue-wide album <-> friendly-name map, built once and invertible."""

    __slots__ = ("_by_cell", "_by_id")
    _by_id: dict[AlbumId, str]  # album id -> its unique friendly name
    _by_cell: dict[str, Album]  # friendly name and ▶-marked name -> album

    def __new__(cls, albums: tuple[Album, ...]) -> Self:
        self = super().__new__(cls)
        self._by_id = cls._assign(albums)
        self._by_cell = {}
        for album in albums:
            name = self._by_id[album.id]
            self._by_cell[name] = album
            self._by_cell[f"{_MARKER}{name}"] = album
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

    def marked_name(self, album: Album, view: PlayerView) -> str:
        """Return the name cell, prefixed with the ``▶`` cue when this album plays."""
        name = self.friendly(album)
        return f"{_MARKER}{name}" if view.album == album.id else name

    def resolve(self, anchor: str) -> Album:
        """Return the album whose name cell the clicked ``anchor`` names, or raise.

        ``anchor`` is the ``key_column`` cell of the selected row -- the plain
        friendly name for an idle album, the ``▶``-prefixed name for the playing
        one -- and the map holds both forms, so a click resolves regardless of
        which album was playing when the row rendered. An anchor that names no
        album is a stale or unknown click and raises (PY-EH-8), so the receive
        boundary drops it rather than playing the wrong album.
        """
        album = self._by_cell.get(anchor)
        if album is None:
            msg = f"music.play anchor {anchor!r} names no catalogued album"
            raise ValueError(msg)
        return album
