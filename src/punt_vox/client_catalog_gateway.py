"""The production :class:`CatalogGateway` -- a WebSocket adapter over ``voxd``.

``ClientCatalogGateway`` maps each catalog-authoring verb onto the matching
session-free ``music_*`` call on a :class:`VoxClientSync`. The client already
parses the daemon's replies, so this adapter holds no policy of its own; the
daemon owns the catalog. It is a structural match for
:class:`~punt_vox.catalog_gateway.CatalogGateway` (no inheritance), mirroring
the playback-side :class:`~punt_vox.client_gateway.ClientProgramGateway`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from punt_vox.client_sync import VoxClientSync

if TYPE_CHECKING:
    from punt_vox.types_programs.prompts import PromptSet

__all__ = ["ClientCatalogGateway"]


@final
class ClientCatalogGateway:
    """Back the ``CatalogGateway`` seam with WebSocket calls to ``voxd``."""

    __slots__ = ("_client",)
    _client: VoxClientSync

    def __new__(cls, client: VoxClientSync) -> Self:
        self = super().__new__(cls)
        self._client = client
        return self

    def new(self, prompts: PromptSet, name: str | None) -> str:
        """Author one track via the ``music_new`` op; return the album id."""
        return self._client.music_new(prompts, name)

    def get(self, album_id: str, dest_dir: str) -> str:
        """Copy the album into *dest_dir* via the ``music_get`` op."""
        return str(self._client.music_get(album_id, Path(dest_dir)))

    def remove(self, album_id: str) -> None:
        """Delete the album via the ``music_remove`` op."""
        self._client.music_remove(album_id)
