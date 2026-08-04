"""``AlbumListScene`` -- the pure projection of (catalog, view) onto a lux scene.

Given the saved-album catalog and the :class:`PlayerView`, build the ``vox.music``
scene, three regions top to bottom: a **now-playing block** (album, track line),
the **transport row**, and the **album table** under a one-line count label. The
lux frame is already titled "Music", so the scene renders no heading of its own.
It is a deterministic, I/O-free function of its inputs (the gate's projection
carve-out), reading only in-memory manifest data. The album table carries a
``publish`` decorator on its row selection -- selecting a row publishes
``music.play``, which :class:`LuxSubscription` decodes on the other leg -- and the
transport buttons each carry their own ``publish`` topic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, final

from punt_lux import RenderRequest, SeparatorElement, TextElement

from punt_vox.voxd.music_player.album_table import AlbumTable
from punt_vox.voxd.music_player.now_playing_block import NowPlayingBlock
from punt_vox.voxd.music_player.playback_notice import PlaybackNotice
from punt_vox.voxd.music_player.transport_row import TransportRow

if TYPE_CHECKING:
    from punt_vox.voxd.music_player.player_view import PlayerView
    from punt_vox.voxd.programs.catalog import Album

__all__ = ["AlbumListScene"]

_SCENE_ID = "vox.music"
_TITLE = "Music"


@final
@dataclass(frozen=True, slots=True)
class AlbumListScene:
    """Project the catalog and the player view onto the ``vox.music`` scene.

    The :class:`PlaybackNotice` is a transient status the scene renders as a one-line
    warning between the now-playing block and the transport; when silent (the
    default) the ``music.status`` slot renders empty, so the scene shape is the same
    whether or not a click failed. It is a value the projection carries, never a flag
    on the view, so :class:`PlayerView`'s invariants stay untouched -- and the
    projection stays a pure function of ``(albums, view, notice)``.
    """

    albums: tuple[Album, ...]
    view: PlayerView
    notice: PlaybackNotice = field(default_factory=PlaybackNotice.silent)

    def render_request(self) -> RenderRequest:
        """Return the whole scene to install: now-playing, transport, album table."""
        elements: list[dict[str, object]] = list(
            NowPlayingBlock(self.view, self.albums).elements()
        )
        elements.append(
            TextElement(id="music.status", content=self.notice.message).to_dict()
        )
        elements.append(TransportRow(self.view).to_dict())
        elements.append(SeparatorElement(id="music.sep").to_dict())
        elements.extend(AlbumTable(self.albums).elements())
        return RenderRequest(scene_id=_SCENE_ID, elements=elements, title=_TITLE)
