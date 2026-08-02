"""``NowPlayingBlock`` -- the scene's top region: what is playing, right now.

Three stacked elements when a source is active: the **album name** (prominent, a
markdown heading), the **track line** (its title on the left, the ``N of M``
position on the right), and a **progress bar**. The bar is the one element meant
to move -- fed by mpv's ``time-pos`` polled ~1/s -- but that poll is deferred, so
it renders static at zero for now; everything else is static per track.

When nothing plays the region collapses to a single ``"Nothing playing"`` line and
the transport greys out (the :class:`TransportRow` renders every button disabled off
the same idle view), so the whole scene reads as quiescent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from punt_lux import MarkdownElement, ProgressElement, TextElement

from punt_vox.voxd.music_player.album_display import AlbumDisplay

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
        """Return the region's wire elements: idle line, or album/track/progress."""
        cursor = self.view.now_playing
        if cursor is None:
            return [self._idle()]
        return [self._album(), self._track_line(cursor), self._progress()]

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
        """Return the display name of the playing album (T7 guarantees it exists)."""
        match = next((a for a in self.albums if a.id == self.view.album), None)
        return "album" if match is None else AlbumDisplay(match).name

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
        """Return the song title, falling back to ``Track N`` until ID3 titles land."""
        return cursor.title or f"Track {cursor.index}"

    @staticmethod
    def _progress() -> dict[str, object]:
        """Return the static progress bar -- zero-filled until time polling lands."""
        return ProgressElement(id="music.now.progress", fraction=0.0).to_dict()
