"""``AlbumListScene`` -- the pure projection of (catalog, view) onto a lux scene.

Given the saved-album catalog and the :class:`PlayerView`, build the ``vox.music``
scene: a now-playing line, a Stop control, and one row per album with its name and a
Play button. It is a deterministic, I/O-free function of its inputs (the gate's
projection carve-out), reading only in-memory manifest data. The Play/Stop buttons
carry the ``publish`` attribute the phase-2 receive leg decodes -- a Play publishes
``music.play`` with its album id, the Stop publishes a bare ``music.stop``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, final

from punt_lux import (
    MarkdownElement,
    RenderRequest,
    SeparatorElement,
    TextElement,
)

from punt_vox.voxd.music_player.album_row import AlbumRow
from punt_vox.voxd.music_player.playback_notice import PlaybackNotice
from punt_vox.voxd.music_player.transport_row import TransportRow

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


@final
@dataclass(frozen=True, slots=True)
class AlbumListScene:
    """Project the catalog and the player view onto the ``vox.music`` scene.

    The :class:`PlaybackNotice` is a transient status the scene renders as a one-line
    warning between the now-playing line and the controls; when silent (the default)
    the ``music.status`` slot renders empty, so the scene shape is the same whether or
    not a click failed. It is a value the projection carries, never a flag on the
    view, so :class:`PlayerView`'s invariants stay untouched -- and the projection
    stays a pure function of ``(albums, view, notice)``.
    """

    albums: tuple[Album, ...]
    view: PlayerView
    notice: PlaybackNotice = field(default_factory=PlaybackNotice.silent)

    def render_request(self) -> RenderRequest:
        """Return the whole scene to install: header, status, controls, album rows."""
        elements: list[_WireElement] = [
            MarkdownElement(id="music.header", content=f"## {_TITLE}"),
            TextElement(id="music.now", content=self._now_playing_text()),
            TextElement(id="music.status", content=self.notice.message),
            TransportRow(self.view),
            SeparatorElement(id="music.sep"),
        ]
        elements.extend(self._album_row(album) for album in self.albums)
        return RenderRequest(
            scene_id=_SCENE_ID,
            elements=[element.to_dict() for element in elements],
            title=_TITLE,
        )

    def _album_row(self, album: Album) -> AlbumRow:
        """Return one album's row: its name (marked if playing) and a Play button."""
        return AlbumRow(album_id=album.id.value, label=self._album_label(album))

    def _album_label(self, album: Album) -> str:
        """Return the album's display name, marked with a cue when it is playing."""
        name = self._display_name(album)
        return f"▶ {name}" if self.view.album == album.id else name

    def _now_playing_text(self) -> str:
        """Return the now-playing marquee text, or an idle placeholder."""
        cursor = self.view.now_playing
        if cursor is None:
            return "Nothing playing"
        return f"▶ {self._playing_name()} — {cursor.index} of {cursor.of}"

    def _playing_name(self) -> str:
        """Return the display name of the playing album, or a bare fallback."""
        match = next((a for a in self.albums if a.id == self.view.album), None)
        return "album" if match is None else self._display_name(match)

    @staticmethod
    def _display_name(album: Album) -> str:
        """Return an album's name, or a bare ``album <id>`` when it is unnamed."""
        return album.manifest.tags.name or f"album {album.id.value}"
