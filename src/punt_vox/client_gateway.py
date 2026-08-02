"""The production :class:`ProgramGateway` -- a WebSocket adapter over ``voxd``.

``ClientProgramGateway`` is the seam between the client surfaces and the wire:
it maps each gateway verb onto the matching session-free ``program_*`` call on a
:class:`VoxClientSync`. The client already parses the daemon's replies into the
domain value objects the surfaces render (:class:`ProgramStatus`,
:class:`CommandOutcome`, :class:`ProgramSummary`), so this gateway holds no
policy of its own -- authoring, rendering, and error presentation stay in the
surfaces; the daemon owns the state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from punt_vox.client_sync import VoxClientSync

if TYPE_CHECKING:
    from punt_vox.types_programs.control import (
        CommandOutcome,
        ProgramSummary,
        SelectionRequest,
        StartRequest,
    )
    from punt_vox.types_programs.status import ProgramStatus

__all__ = ["ClientProgramGateway"]


@final
class ClientProgramGateway:
    """Back the ``ProgramGateway`` seam with WebSocket calls to ``voxd``."""

    __slots__ = ("_client",)
    _client: VoxClientSync

    def __new__(cls, client: VoxClientSync) -> Self:
        self = super().__new__(cls)
        self._client = client
        return self

    def status(self) -> ProgramStatus:
        """Return the daemon's authoritative Program status, read fresh."""
        return self._client.program_status()

    def start(self, request: StartRequest) -> CommandOutcome:
        """Turn a Program on from the authored ``request`` (carrying the vibe)."""
        return self._client.program_on(
            style=request.style,
            vibe=request.vibe,
            name=request.name,
            prompts=request.prompts,
        )

    def stop(self) -> CommandOutcome:
        """Halt the active Program."""
        return self._client.program_stop()

    def advance(self) -> CommandOutcome:
        """User transport next: step the replay cursor forward, or skip a Program."""
        return self._client.program_next()

    def prev(self) -> CommandOutcome:
        """User transport prev: step the replay cursor back one part."""
        return self._client.program_prev()

    def pause(self) -> CommandOutcome:
        """Suspend the active source in place (transport pause)."""
        return self._client.program_pause()

    def resume(self) -> CommandOutcome:
        """Continue a suspended source (transport resume)."""
        return self._client.program_resume()

    def select(self, request: SelectionRequest) -> CommandOutcome:
        """Replay a Selection resolved by id (direct) or by tags."""
        return self._client.program_select(
            style=request.style,
            vibe=request.vibe,
            name=request.name,
            album_id=request.id,
        )

    def catalog(self) -> tuple[ProgramSummary, ...]:
        """Return every album, parsed from the ``program_list`` reply."""
        return self._client.program_list()
