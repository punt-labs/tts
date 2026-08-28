"""``NowPlayingBlock`` -- the scene's top region: what is playing, right now.

Two stacked slots, always both present: the **album name** (prominent, a markdown
heading) and the **position line** (the ``N of M`` slot in the pool). There is no
song-title line: the per-track title carried the generation prompt, not a song
name, so it read as a wall of text next to the position and told the user nothing
-- the album name above already names what is playing. There is no progress bar
either: a live one would force voxd to push mpv's ``time-pos`` on a constant timer
for a sliver of information, so the block carries only what is static per track.

Idle changes the slots' *content*, never their number: the heading reads "Nothing
playing" and the position line renders empty (the transport greys out alongside,
off the same idle view, so the whole scene reads as quiescent). That is better
information design -- the question each slot answers stays at one position and one
type scale, so the reader's eye does not have to re-find "what is playing?" every
time playback starts or stops -- and it is the same trade ``music.status`` and
``vox.panel.status`` already make.

It is also what lets a track change refresh the widget without re-installing it.
A patch can set fields on an installed element but cannot add one, so a region
that emitted one element when idle and two when active would force a full,
frame-raising re-install at exactly the moment the user pressed play. One roster,
always.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from punt_lux import MarkdownElement, TextElement

from punt_vox.voxd.music_player.album_names import AlbumNames

if TYPE_CHECKING:
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
        """Return the region's two slots, always both: the album, then the position."""
        return [self._album(), self._position()]

    def _album(self) -> dict[str, object]:
        """Return the prominent heading slot, its text escaped inert (F#).

        The album name is untrusted (agent/user-influenced), so it is escaped
        through :class:`MarkdownText` before it is interpolated into the heading:
        the ``### `` marker keeps the prominence, the name cannot inject markup.
        """
        heading = MarkdownText(self._heading()).text
        return MarkdownElement(id="music.now.album", content=f"### {heading}").to_dict()

    def _heading(self) -> str:
        """Return the heading's text: the playing album's name, or the idle line."""
        if self.view.now_playing is None:
            return _IDLE_TEXT
        return self._playing_name()

    def _playing_name(self) -> str:
        """Return the friendly name of the playing album (T7 guarantees it exists).

        The name comes from the catalogue-wide :class:`AlbumNames` map so it is the
        same unique friendly name the album's table row shows, not a bare re-derive
        that could drift from it.
        """
        match = next((a for a in self.albums if a.id == self.view.album), None)
        return "album" if match is None else AlbumNames(self.albums).friendly(match)

    def _position(self) -> dict[str, object]:
        """Return the ``N of M`` position slot, empty when nothing is playing."""
        cursor = self.view.now_playing
        content = "" if cursor is None else f"{cursor.index} of {cursor.of}"
        return TextElement(id="music.now.position", content=content).to_dict()
