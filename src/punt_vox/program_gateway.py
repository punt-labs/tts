"""The daemon-facing seam every client surface calls -- ``ProgramGateway``.

The surfaces (the ``mic`` MCP tools and the ``vox music`` CLI) are thin adapters
over this one Protocol: they translate MCP/CLI semantics into gateway calls and
render what comes back, holding no daemon logic themselves. Production backs it
with :class:`~punt_vox.client_gateway.ClientProgramGateway` (WebSocket to
``voxd``); tests inject an in-memory fake. No method takes a session or an owner
-- ``voxd`` state is machine-universal, so any client drives any command and
every client can read the authoritative status (design section 4).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from punt_vox.types_programs.control import (
    CommandOutcome,
    ProgramSummary,
    SelectionRequest,
    StartRequest,
)
from punt_vox.types_programs.status import ProgramStatus

__all__ = ["ProgramGateway"]


@runtime_checkable
class ProgramGateway(Protocol):
    """The set of Program operations a client surface issues against ``voxd``."""

    def status(self) -> ProgramStatus:
        """Return the daemon's authoritative Program status, read fresh per call.

        Never a client-side cache: a caller asking "what is playing?" gets what
        the daemon actually holds, so no stale shadow can drift.
        """
        ...

    def start(self, request: StartRequest) -> CommandOutcome:
        """Turn a Program on from the authored ``request`` (the ``music on`` path)."""
        ...

    def stop(self) -> CommandOutcome:
        """Turn the active Program off."""
        ...

    def advance(self) -> CommandOutcome:
        """User transport next: step the replay cursor forward, or skip a Program."""
        ...

    def prev(self) -> CommandOutcome:
        """User transport prev: step the replay cursor back one part."""
        ...

    def pause(self) -> CommandOutcome:
        """Suspend the active source in place (transport pause)."""
        ...

    def resume(self) -> CommandOutcome:
        """Continue a suspended source (transport resume)."""
        ...

    def select(self, request: SelectionRequest) -> CommandOutcome:
        """Replay a Selection resolved by id (direct) or by tags (the ``play`` path)."""
        ...

    def catalog(self) -> tuple[ProgramSummary, ...]:
        """Return every album, grouped for a ``list`` surface."""
        ...
