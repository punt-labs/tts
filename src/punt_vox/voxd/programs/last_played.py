"""The last-played-album register a no-argument ``play`` repeats."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from punt_vox.voxd.programs.album_id import AlbumId

__all__ = ["LastPlayed"]


@final
class LastPlayed:
    """Remember the last single album replayed so a bare ``play`` can repeat it.

    Daemon-owned and ephemeral: a fresh register holds nothing, so the first
    bare ``play`` after a daemon restart reports that no album has played yet
    rather than silently falling back to the first catalogued album.
    """

    __slots__ = ("_album",)
    _album: AlbumId | None

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._album = None
        return self

    def remember(self, album_id: AlbumId) -> None:
        """Record ``album_id`` as the album a later bare ``play`` repeats."""
        self._album = album_id

    def require(self) -> AlbumId:
        """Return the last-played album id, or raise when none has played yet.

        The bare-``play`` contract is to *repeat an album*; with no history there
        is no album to return, so this raises (PY-EH-8) and the surface renders
        the message beside the catalog rather than playing an arbitrary album.
        """
        if self._album is None:
            msg = "no album played yet; specify an album by id, name, or style/vibe"
            raise ValueError(msg)
        return self._album
