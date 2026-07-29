"""``PlayerView`` -- voxd's one active source projected onto the player scene.

The view is the object the Z model ``docs/vox-music-player.tex`` pins. It is a
two-mode value (idle or one album playing) carrying the playing album's catalog
id and the now-playing cursor, built from a :class:`ProgramStatus` and the current
catalog. Its constructor enforces the modelled invariants so an inconsistent view
cannot be built:

* **I1** at most one album playing -- ``album`` is a single id or ``None``.
* **I2** now-playing present iff playing -- ``mode`` is ``playing`` exactly when an
  album and a cursor are present.
* **I3** a played album is catalogued -- :meth:`from_status` only names an album it
  matched in the catalog, so the view never reports an unknown album.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from punt_vox.types_programs.status import ProgramStatus
    from punt_vox.types_programs.status_views import NowPlaying
    from punt_vox.voxd.programs.album_id import AlbumId
    from punt_vox.voxd.programs.catalog import Album

__all__ = ["PlayerMode", "PlayerView"]


class PlayerMode(StrEnum):
    """The player's two modes: nothing playing, or one saved album playing."""

    IDLE = "idle"
    PLAYING = "playing"


@final
@dataclass(frozen=True, slots=True)
class PlayerView:
    """The projection of the one active source: mode, album (<=1), and cursor."""

    mode: PlayerMode
    album: AlbumId | None  # the single catalogued album playing, None when idle
    now_playing: NowPlaying | None  # the "Part N of M" cursor, None when idle

    def __post_init__(self) -> None:
        """Enforce I2: playing <=> an album is present <=> a cursor is present."""
        playing = self.mode is PlayerMode.PLAYING
        if playing != (self.album is not None) or playing != (
            self.now_playing is not None
        ):
            msg = (
                "inconsistent PlayerView: mode, album, and now_playing must agree "
                f"(mode={self.mode}, album={self.album}, "
                f"now_playing={self.now_playing})"
            )
            raise ValueError(msg)

    @classmethod
    def idle(cls) -> Self:
        """Return the idle view -- nothing playing (I2 in its empty shape)."""
        return cls(mode=PlayerMode.IDLE, album=None, now_playing=None)

    @classmethod
    def from_status(cls, status: ProgramStatus, albums: tuple[Album, ...]) -> Self:
        """Project the daemon status onto the view against the current catalog.

        The view is ``playing`` only when the daemon reports a live cursor *and*
        the active source resolves to exactly one catalogued album (I3). A radio
        that spans several albums, or any source the catalog does not name, reads
        as idle here -- the player scene models single-album playback, not the
        generative radio.
        """
        if status.now_playing is None:
            return cls.idle()
        album = cls._playing_album(status, albums)
        if album is None:
            return cls.idle()
        return cls(
            mode=PlayerMode.PLAYING, album=album.id, now_playing=status.now_playing
        )

    @staticmethod
    def _playing_album(
        status: ProgramStatus, albums: tuple[Album, ...]
    ) -> Album | None:
        """Return the one catalogued album backing the status, or None.

        The active source's status handle is the album's directory locator (both a
        single-album replay and a generate Program report it), so a locator match
        against the catalog names the playing album; no match means a multi-album
        radio or a source outside the catalog.
        """
        name = status.name
        if name is None:
            return None
        return next((a for a in albums if a.locator == name.value), None)
