"""``AlbumListScene`` -- the pure projection of (catalog, view) onto a lux scene.

Given the saved-album catalog and the :class:`PlayerView`, build the ``vox.music``
scene: a now-playing line, a Stop control, and one row per album with its name and
a Play button. It is a deterministic, I/O-free function of its inputs (the gate's
projection carve-out), reading only in-memory manifest data. The Play/Stop buttons
carry stable ids the phase-2 receive leg decodes; phase 1 renders them inert.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, final

from punt_lux import (
    ButtonElement,
    GroupElement,
    MarkdownElement,
    RenderRequest,
    SeparatorElement,
    TextElement,
)

if TYPE_CHECKING:
    from punt_vox.voxd.music_player.player_view import PlayerView
    from punt_vox.voxd.programs.catalog import Album

__all__ = ["AlbumListScene"]


class _WireElement(Protocol):
    """The structural slice of a lux element the scene needs: its wire form.

    ``punt_lux`` does not publicly export the element Protocol, so the scene names
    the one method it uses -- ``to_dict`` -- structurally (PY-TS-6), which every
    concrete element class satisfies.
    """

    def to_dict(self) -> dict[str, object]:
        """Return the element's JSON-compatible wire representation."""
        ...


_SCENE_ID = "vox.music"
_TITLE = "Music"
_STOP_ID = "music.stop"
_PLAY_PREFIX = "music.play."


@final
@dataclass(frozen=True, slots=True)
class AlbumListScene:
    """Project the catalog and the player view onto the ``vox.music`` scene."""

    albums: tuple[Album, ...]
    view: PlayerView

    def render_request(self) -> RenderRequest:
        """Return the whole scene to install: the header, controls, and album rows."""
        elements: list[_WireElement] = [
            MarkdownElement(id="music.header", content=f"## {_TITLE}"),
            TextElement(id="music.now", content=self._now_playing_text()),
            ButtonElement(id=_STOP_ID, label="Stop", disabled=not self._is_playing()),
            SeparatorElement(id="music.sep"),
        ]
        elements.extend(self._album_row(album) for album in self.albums)
        return RenderRequest(
            scene_id=_SCENE_ID,
            elements=[element.to_dict() for element in elements],
            title=_TITLE,
        )

    def _album_row(self, album: Album) -> GroupElement:
        """Return one album's row: its name (marked if playing) and a Play button."""
        aid = album.id.value
        return GroupElement(
            id=f"music.row.{aid}",
            layout="columns",
            children=(
                TextElement(id=f"music.name.{aid}", content=self._album_label(album)),
                ButtonElement(id=f"{_PLAY_PREFIX}{aid}", label="Play"),
            ),
        )

    def _album_label(self, album: Album) -> str:
        """Return the album's display name, marked with a cue when it is playing."""
        name = album.manifest.tags.name or f"album {album.id.value}"
        return f"▶ {name}" if self.view.album == album.id else name

    def _now_playing_text(self) -> str:
        """Return the now-playing marquee text, or an idle placeholder."""
        cursor = self.view.now_playing
        if cursor is None:
            return "Nothing playing"
        label = self._playing_name()
        return f"▶ {label} — {cursor.index} of {cursor.of}"

    def _playing_name(self) -> str:
        """Return the display name of the playing album, or a bare fallback."""
        playing = self.view.album
        match = next((a for a in self.albums if a.id == playing), None)
        if match is None:
            return "album"
        return match.manifest.tags.name or f"album {match.id.value}"

    def _is_playing(self) -> bool:
        """Return whether a saved album is currently playing."""
        return self.view.now_playing is not None
