"""The structural ports the music player depends on: the daemon seam and the sink.

The player reads voxd's state through :class:`PlayerService` (status + catalog)
and hands each freshly projected scene to a :class:`ScenePublisher`. Both are
``Protocol``s (PY-TS-6) so a test drives the player with an in-memory fake service
and a fake publisher, and the daemon injects the real ``ProgramService`` and
``LuxScenePublisher`` at the composition root.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from punt_lux import RenderRequest

    from punt_vox.types_programs.status import ProgramStatus
    from punt_vox.voxd.programs.catalog import Album

__all__ = ["PlayerService", "ScenePublisher"]


@runtime_checkable
class PlayerService(Protocol):
    """The read-only daemon seam the player projects onto a scene."""

    def status(self) -> ProgramStatus:
        """Return the daemon's authoritative status, read fresh per call."""
        ...

    def catalog_albums(self) -> tuple[Album, ...]:
        """Return every saved album, newest first (the catalog list view)."""
        ...


@runtime_checkable
class ScenePublisher(Protocol):
    """A non-blocking sink the player hands each rendered scene to.

    Two verbs, because a push carries an intent as well as a tree. A refresh
    updates a window the user is already looking at and must leave its stacking
    order alone; an install puts the window in front of the user, which is what a
    menu click asks for and what a fresh hub connection needs -- the concrete
    sink backs an install with both a ``show`` push and an explicit frame raise,
    because ``show`` alone does not reliably raise a frame already installed
    (DES-072 addendum).
    """

    def submit(self, request: RenderRequest) -> None:
        """Accept the newest scene as a refresh, without blocking the caller."""
        ...

    def reinstall(self, request: RenderRequest) -> None:
        """Accept the newest scene as an install -- shown, and its frame raised."""
        ...
