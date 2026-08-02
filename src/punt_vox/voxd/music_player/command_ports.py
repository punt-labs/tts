"""The write-side daemon seam the receive leg drives, and the combined seam.

The player's *read* seam lives in :mod:`ports` (:class:`PlayerService`: status +
catalog). This module adds the *write* seam a click needs -- :class:`PlayerCommands`
(replay an album, or turn the source off) -- and :class:`ProgramSeam`, the union of
the two that the composition root types its one ``ProgramService`` as, so it can hand
the same object to the read-only player and the write-only subscription and mypy sees
each half structurally.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from punt_vox.voxd.music_player.ports import PlayerService

if TYPE_CHECKING:
    from punt_vox.voxd.programs.album_id import AlbumId

__all__ = ["PlayerCommands", "ProgramSeam"]


@runtime_checkable
class PlayerCommands(Protocol):
    """The write seam an inbound event applies: play, stop, and the transport."""

    def replay_album(self, album_id: AlbumId) -> None:
        """Replay the single saved album named by ``album_id`` (start or switch)."""
        ...

    def stop(self) -> None:
        """Halt the active source, returning the player to idle (Stop)."""
        ...

    def advance(self) -> None:
        """User transport next: step the replay cursor forward (Next)."""
        ...

    def prev(self) -> None:
        """User transport prev: step the replay cursor back (Prev)."""
        ...

    def pause(self) -> None:
        """Suspend the active source in place (Pause)."""
        ...

    def resume(self) -> None:
        """Continue a suspended source (Resume)."""
        ...


@runtime_checkable
class ProgramSeam(PlayerService, PlayerCommands, Protocol):
    """The full daemon seam: the read side (:class:`PlayerService`) plus commands."""
