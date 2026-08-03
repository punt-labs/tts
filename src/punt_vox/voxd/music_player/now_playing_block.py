"""``NowPlayingBlock`` -- the scene's top region: what is playing, right now.

Two stacked elements when a source is active: the **album name** (prominent, a
markdown heading) and the **track line** (the song title on the left, the ``N of
M`` position on the right). There is no progress bar: a live one would force voxd
to push mpv's ``time-pos`` on a constant timer for a sliver of information, so the
block carries only what is static per track -- the album, the song, and the
position.

When nothing plays the region collapses to a single ``"Nothing playing"`` line and
the transport greys out (the :class:`TransportRow` renders every button disabled off
the same idle view), so the whole scene reads as quiescent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from punt_lux import MarkdownElement, TextElement

from punt_vox.voxd.music_player.album_names import AlbumNames

if TYPE_CHECKING:
    from punt_vox.types_programs.status_views import NowPlaying
    from punt_vox.voxd.music_player.player_view import PlayerView
    from punt_vox.voxd.programs.catalog import Album

__all__ = ["NowPlayingBlock"]

_IDLE_TEXT = "Nothing playing"


@final
@dataclass(frozen=True, slots=True)
class NowPlayingBlock:
    """Project the active source onto the scene's top now-playing region."""

    view: PlayerView
    albums: tuple[Album, ...]

    def elements(self) -> list[dict[str, object]]:
        """Return the region's wire elements: the idle line, or album + track line."""
        cursor = self.view.now_playing
        if cursor is None:
            return [self._idle()]
        return [self._album(), self._track_line(cursor)]

    @staticmethod
    def _idle() -> dict[str, object]:
        """Return the single idle line shown when nothing is playing."""
        return TextElement(id="music.now", content=_IDLE_TEXT).to_dict()

    def _album(self) -> dict[str, object]:
        """Return the prominent album-name heading of the playing source."""
        return MarkdownElement(
            id="music.now.album", content=f"### {self._playing_name()}"
        ).to_dict()

    def _playing_name(self) -> str:
        """Return the friendly name of the playing album (T7 guarantees it exists).

        The name comes from the catalogue-wide :class:`AlbumNames` map so it is the
        same unique friendly name the album's table row shows, not a bare re-derive
        that could drift from it.
        """
        match = next((a for a in self.albums if a.id == self.view.album), None)
        return "album" if match is None else AlbumNames(self.albums).friendly(match)

    def _track_line(self, cursor: NowPlaying) -> dict[str, object]:
        """Return the track line: the song title left, the ``N of M`` position right."""
        title = TextElement(id="music.now.track", content=self._track_title(cursor))
        position = TextElement(
            id="music.now.position", content=f"{cursor.index} of {cursor.of}"
        )
        return {
            "kind": "group",
            "id": "music.now.line",
            "layout": "columns",
            "children": [title.to_dict(), position.to_dict()],
        }

    @staticmethod
    def _track_title(cursor: NowPlaying) -> str:
        """Return the song title, or ``Track N`` when no ID3 title exists."""
        return cursor.title or f"Track {cursor.index}"
