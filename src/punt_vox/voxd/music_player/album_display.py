"""``AlbumDisplay`` -- one album's presentation vocabulary for the ``vox.music`` scene.

The scene renders an album three ways -- the now-playing name, an album-table row,
and (inverted) the click that selects it -- and each needs the same derived strings:
the friendly name (or the ``album <id>`` fallback the generator leaves until it
writes real ID3), the genre, the track count, and the now-playing marker. Those
derivations live here, on the object that owns an album's display, so no free
function reaches into ``Album``'s manifest to recompute them (PY-OO-7).

The class also owns the *inverse*: :meth:`resolve` maps a clicked row's key cell --
the album's displayed name, marked or not -- back to its catalogued ``Album``. The
album table's ``key_column`` is the display-name column, so a row selection arrives
as that name (lux delivers it as ``payload['anchor']``); voxd owns the name-to-id
mapping and resolves it here, which is why the table needs no id column and keeps the
three-column Album/Genre/Tracks shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from punt_vox.voxd.music_player.player_view import PlayerView
    from punt_vox.voxd.programs.catalog import Album

__all__ = ["AlbumDisplay"]

_MARKER = "▶ "  # the now-playing cue prefixed to the playing album's name cell


@final
@dataclass(frozen=True, slots=True)
class AlbumDisplay:
    """The display projection of one catalogued album: its cells and its cue."""

    album: Album

    @property
    def id(self) -> str:
        """Return the album's opaque id string (never shown; the resolution target)."""
        return self.album.id.value

    @property
    def name(self) -> str:
        """Return the friendly title, or ``album <id>`` when the generator wrote none.

        The unnamed fallback embeds the id, so it is still unique -- an unnamed
        album's row remains a resolvable key cell like a named one's.
        """
        return self.album.manifest.tags.name or f"album {self.id}"

    @property
    def genre(self) -> str:
        """Return the album's style tag -- the Genre column cell."""
        return self.album.manifest.tags.style

    @property
    def track_count(self) -> int:
        """Return the number of manifest parts -- the Tracks column cell."""
        return len(self.album.manifest.parts)

    def marked_name(self, view: PlayerView) -> str:
        """Return the name cell, prefixed with the ``▶`` cue when this album plays."""
        return f"{_MARKER}{self.name}" if view.album == self.album.id else self.name

    @classmethod
    def resolve(cls, anchor: str, albums: tuple[Album, ...]) -> Album:
        """Return the album whose name cell the clicked ``anchor`` names, or raise.

        ``anchor`` is the ``key_column`` cell of the selected row, which is the
        album's :meth:`marked_name` -- the plain name for an idle album, the
        ``▶``-prefixed name for the playing one. Matching against both forms
        resolves the click regardless of which album was playing when the row was
        rendered, without depending on the render-time view. A name that matches no
        album is a stale or unknown click and raises (PY-EH-8), so the receive
        boundary drops it rather than playing the wrong album.
        """
        for album in albums:
            display = cls(album)
            if anchor in (display.name, f"{_MARKER}{display.name}"):
                return album
        msg = f"music.play anchor {anchor!r} names no catalogued album"
        raise ValueError(msg)
