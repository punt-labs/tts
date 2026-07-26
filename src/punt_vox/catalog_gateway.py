"""The catalog-authoring seam the ``vox music`` new/get/remove verbs call.

Distinct from :class:`~punt_vox.program_gateway.ProgramGateway` (playback
control): these operations mutate the saved-album catalog, not the running
Program. The surface is a thin adapter over this Protocol; production backs it
with :class:`~punt_vox.client_catalog_gateway.ClientCatalogGateway` (WebSocket to
``voxd``), while a test injects an in-memory fake. The daemon owns the catalog
and every path decision.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from punt_vox.types_programs.prompts import PromptSet

__all__ = ["CatalogGateway"]


@runtime_checkable
class CatalogGateway(Protocol):
    """The catalog-authoring operations ``music new``/``get``/``remove`` issue."""

    def new(self, prompts: PromptSet, name: str | None) -> str:
        """Author one track from *prompts* into a fresh album; return its id.

        *prompts* is the one-track :class:`PromptSet` both surfaces build via
        :meth:`PromptSet.single`, so the daemon receives the authored-input object
        rather than a bare string.
        """
        ...

    def get(self, album_id: str, dest_dir: str) -> str:
        """Copy album *album_id* into *dest_dir*; return the written directory."""
        ...

    def remove(self, album_id: str) -> None:
        """Delete album *album_id* from the catalog (a live album is refused)."""
        ...
