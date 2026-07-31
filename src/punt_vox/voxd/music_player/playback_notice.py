"""``PlaybackNotice`` -- a transient user-facing status the scene projection carries.

A Play or Stop the user clicked can fail to apply: the album vanished from the
catalog, or it has no ready tracks yet. Such a failure changes no daemon state, so
no change signal fires and the scene would otherwise show nothing at all -- the click
would look ignored. The receive leg instead builds a warning notice and drives a
re-push carrying it, and the scene renders it as a one-line status.

The notice is a Null Object (PY-DP-9): :meth:`silent` is the normal state and renders
no status line; :meth:`warning` carries a message. It is deliberately transient -- the
next legitimate projection (a successful play, a stop, a catalog edit) carries the
silent notice and clears the warning. It is a *value the scene carries*, never a flag
on the :class:`PlayerView`, so the modelled view invariants stay untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from punt_vox.voxd.programs.album_id import AlbumId
    from punt_vox.voxd.programs.catalog import Album

__all__ = ["PlaybackNotice"]

_STOP_FAILED = "⚠ couldn't stop the music"


@final
@dataclass(frozen=True, slots=True)
class PlaybackNotice:
    """A transient scene status: a warning message, or silent (the Null state).

    The named constructors phrase every warning here, so all the user-facing failure
    text lives in one place and the player only decides *when* to raise one.
    """

    message: str  # the empty string is the silent Null state -- no status line

    @classmethod
    def silent(cls) -> Self:
        """Return the silent notice -- the normal state, rendering no status line."""
        return cls("")

    @classmethod
    def warning(cls, message: str) -> Self:
        """Return a warning notice carrying ``message`` for the scene status line."""
        return cls(message)

    @classmethod
    def play_failed(cls, album: AlbumId, albums: tuple[Album, ...]) -> Self:
        """Return the warning for a play that could not run, catalogued or vanished.

        Catalog presence -- not the exception text -- tells the two cases apart: a
        still-catalogued album that refused to play had no ready tracks; an album no
        lookup finds vanished from the crate.
        """
        match = next((a for a in albums if a.id == album), None)
        if match is None:
            gone = f"⚠ couldn't play {album.value} — no longer in the crate"
            return cls.warning(gone)
        name = match.manifest.tags.name or f"album {album.value}"
        return cls.warning(f"⚠ couldn't play {name} — it has no tracks yet")

    @classmethod
    def stop_failed(cls) -> Self:
        """Return the warning for a stop that could not apply."""
        return cls.warning(_STOP_FAILED)

    @property
    def is_present(self) -> bool:
        """Return whether a status line should render (a non-empty message)."""
        return bool(self.message)
