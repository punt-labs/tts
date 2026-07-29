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
    from punt_lux import OpError, RenderRequest, SceneShown

    from punt_vox.types_programs.status import ProgramStatus
    from punt_vox.voxd.programs.catalog import Album

__all__ = ["LuxRenderer", "PlayerService", "ScenePublisher"]


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
    """A non-blocking sink the player submits each rendered scene to (PY-DP-11)."""

    def submit(self, request: RenderRequest) -> None:
        """Accept the newest scene without blocking the caller's thread."""
        ...


@runtime_checkable
class LuxRenderer(Protocol):
    """The public lux client surface the scene publisher renders through (PY-DP-11).

    The concrete ``LuxRestClient`` satisfies this structurally; depending on the
    Protocol -- not the ``@final`` client -- lets the publisher be tested with a
    fake renderer and keeps the raw-REST client out of the publisher's contract.
    """

    def render(self, request: RenderRequest) -> SceneShown | OpError:
        """Install a whole scene, returning the result or a typed error."""
        ...
