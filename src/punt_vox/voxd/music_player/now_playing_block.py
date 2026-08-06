"""``NowPlayingBlock`` -- the scene's top region: what is playing, right now.

Two stacked elements when a source is active: the **album name** (prominent, a
markdown heading) and the **position line** (the ``N of M`` slot in the pool).
There is no song-title line: the per-track title carried the generation prompt,
not a song name, so it read as a wall of text next to the position and told the
user nothing -- the album name above already names what is playing. There is no
progress bar either: a live one would force voxd to push mpv's ``time-pos`` on a
constant timer for a sliver of information, so the block carries only what is
static per track -- the album and the position.

When nothing plays the region collapses to a single ``"Nothing playing"`` line and
the transport greys out (the :class:`TransportRow` renders every button disabled off
the same idle view), so the whole scene reads as quiescent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from punt_lux import MarkdownElement, TextElement

from punt_vox.voxd.music_player.album_names import AlbumNames

if TYPE_CHECKING:
    from punt_vox.types_programs.status_views import NowPlaying
    from punt_vox.voxd.music_player.player_view import PlayerView
    from punt_vox.voxd.programs.catalog import Album

__all__ = ["NowPlayingBlock"]

_IDLE_TEXT = "Nothing playing"

# Every CommonMark backslash-escapable ASCII punctuation char. A backslash
# before any of these renders it as a literal, so escaping the whole set (plus
# folding line breaks) neutralises heading, emphasis, link, code, and raw-HTML
# injection from an untrusted album name.
_MD_PUNCTUATION: frozenset[str] = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


@final
class MarkdownText:
    """An untrusted string escaped so markdown paints it as inert literal text.

    The now-playing album name is agent/user-influenced. Interpolated raw into a
    ``### {name}`` heading it could inject markdown -- a forged heading via an
    embedded newline, emphasis, a link, or raw HTML. Construction backslash-
    escapes every CommonMark-escapable ASCII punctuation char and folds line
    breaks to spaces, so the name renders under the heading with no markup of its
    own while the ``### `` marker (added by the caller after escaping) keeps its
    prominence.
    """

    __slots__ = ("_text",)
    _text: str

    def __new__(cls, raw: str) -> Self:
        self = super().__new__(cls)
        self._text = cls._escaped(raw)
        return self

    @property
    def text(self) -> str:
        """Return the escaped text, safe to interpolate into a markdown heading."""
        return self._text

    @staticmethod
    def _escaped(raw: str) -> str:
        """Fold line breaks to spaces, then backslash-escape markdown punctuation."""
        folded = raw.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        return "".join(f"\\{ch}" if ch in _MD_PUNCTUATION else ch for ch in folded)


@final
@dataclass(frozen=True, slots=True)
class NowPlayingBlock:
    """Project the active source onto the scene's top now-playing region."""

    view: PlayerView
    albums: tuple[Album, ...]

    def elements(self) -> list[dict[str, object]]:
        """Return the region's wire elements: the idle line, or album + position."""
        cursor = self.view.now_playing
        if cursor is None:
            return [self._idle()]
        return [self._album(), self._position(cursor)]

    @staticmethod
    def _idle() -> dict[str, object]:
        """Return the single idle line shown when nothing is playing."""
        return TextElement(id="music.now", content=_IDLE_TEXT).to_dict()

    def _album(self) -> dict[str, object]:
        """Return the prominent album-name heading, the name escaped inert (F#).

        The album name is untrusted (agent/user-influenced), so it is escaped
        through :class:`MarkdownText` before it is interpolated into the heading:
        the ``### `` marker keeps the prominence, the name cannot inject markup.
        """
        heading = MarkdownText(self._playing_name()).text
        return MarkdownElement(id="music.now.album", content=f"### {heading}").to_dict()

    def _playing_name(self) -> str:
        """Return the friendly name of the playing album (T7 guarantees it exists).

        The name comes from the catalogue-wide :class:`AlbumNames` map so it is the
        same unique friendly name the album's table row shows, not a bare re-derive
        that could drift from it.
        """
        match = next((a for a in self.albums if a.id == self.view.album), None)
        return "album" if match is None else AlbumNames(self.albums).friendly(match)

    @staticmethod
    def _position(cursor: NowPlaying) -> dict[str, object]:
        """Return the ``N of M`` position line -- the playing slot in the pool."""
        return TextElement(
            id="music.now.position", content=f"{cursor.index} of {cursor.of}"
        ).to_dict()
