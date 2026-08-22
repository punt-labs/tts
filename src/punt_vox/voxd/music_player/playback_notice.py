"""``PlaybackNotice`` -- a transient user-facing status the scene projection carries.

A Play or Stop the user clicked can fail to apply: the album vanished from the
catalog, or it has no ready tracks yet. Such a failure changes no daemon state, so
no change signal fires and the scene would otherwise show nothing at all -- the click
would look ignored. The receive leg instead builds a warning notice and drives a
re-push carrying it, and the scene renders it as a one-line status.

The base :class:`LuxNotice` owns the Null-Object shape (PY-DP-9) -- silence, warning,
equality, and the ``is_present`` predicate. This subclass adds the music-domain
factory constructors that phrase every warning in one place, so the player only
decides *when* to raise one. The notice is a *value the scene carries*, never a flag
on the :class:`PlayerView`, so the modelled view invariants stay untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from punt_vox.lux_common import LuxNotice

if TYPE_CHECKING:
    from punt_vox.voxd.programs.album_id import AlbumId
    from punt_vox.voxd.programs.catalog import Album

__all__ = ["PlaybackNotice"]

_STOP_FAILED = "⚠ couldn't stop the music"


@final
class PlaybackNotice(LuxNotice):
    """The music player's :class:`LuxNotice` -- silent, or one of the named warnings."""

    __slots__ = ()

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

    @classmethod
    def resolve_failed(cls, anchor: str) -> Self:
        """Return the warning for a play whose anchor names no catalogued album.

        The click carried a real name -- a row the user just saw -- but the
        catalog no longer holds it (a vanished album, or a stale row cache the
        click resolved against). Different from :meth:`play_failed`, which
        names an already-resolved album that refused to play; here nothing
        resolved in the first place, so the message names the anchor text the
        user clicked, not an album id.
        """
        return cls.warning(f"⚠ couldn't play {anchor} — no longer in the crate")
