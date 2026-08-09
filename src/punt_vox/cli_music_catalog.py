"""The ``vox music`` catalog-authoring verbs: ``new``, ``get``, ``remove``.

These three mutate the saved-album catalog on disk and never touch the running
Program -- no gateway, no style register, no control-verb rendering -- so they
share no state with the playback verbs they sit beside in the Typer group. Held
apart on that seam, mirroring the ``CatalogVerbs``/``MusicTool`` split the MCP
surface already makes, so the two surfaces decompose the same way.

Each verb reports the same fields its ``mic:music`` counterpart returns; the
parity is asserted in ``tests/test_music_surface_parity.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self, final

import typer

from punt_vox.cli_guard import CliGuard
from punt_vox.cli_io import OutputFlags
from punt_vox.client_catalog_gateway import ClientCatalogGateway
from punt_vox.client_sync import VoxClientSync
from punt_vox.types_programs.control import StartRequest
from punt_vox.types_programs.prompts import PromptSet

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.catalog_gateway import CatalogGateway
    from punt_vox.output_formatter import OutputFormatter

__all__ = ["CatalogCli"]

_JsonOutput = Annotated[bool, typer.Option("--json", help="Output JSON.")]
_Verbose = Annotated[
    bool, typer.Option("--verbose", "-v", help="Enable debug logging.")
]
_Quiet = Annotated[
    bool, typer.Option("--quiet", "-q", help="Suppress non-JSON output.")
]


@final
class CatalogCli:
    """Author, export, and delete saved albums from the ``vox music`` group.

    A humble object: every verb crosses to ``voxd`` through the catalog gateway,
    which arrives as a factory called per invocation so a caller that re-points
    the daemon connection between calls is honoured on the next one.
    """

    __slots__ = ("_flags", "_formatter", "_gateway_factory", "_guard")
    _formatter: OutputFormatter
    _gateway_factory: Callable[[], CatalogGateway]
    _flags: OutputFlags
    _guard: CliGuard

    def __new__(
        cls,
        formatter: OutputFormatter,
        gateway_factory: Callable[[], CatalogGateway] | None = None,
        flags: OutputFlags | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._formatter = formatter
        self._gateway_factory = gateway_factory or cls._default_gateway
        self._flags = flags if flags is not None else OutputFlags(formatter)
        self._guard = CliGuard(formatter)
        return self

    @staticmethod
    def _default_gateway() -> CatalogGateway:
        """Build the production catalog gateway -- a fresh client per command."""
        return ClientCatalogGateway(VoxClientSync())

    def new(
        self,
        prompt: Annotated[
            str, typer.Argument(help="Verbatim ElevenLabs descriptive prompt.")
        ],
        title: Annotated[
            str | None, typer.Option("--title", help="Human album title.")
        ] = None,
        *,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """Generate one track into a fresh catalog album; print its bare id.

        The prompt is wrapped as a single-track :class:`PromptSet` -- the same
        authored-input object the MCP tool builds -- and passed to the daemon
        verbatim (no LLM expansion). This parks a track in the catalog and leaves
        the active Program untouched. A blank prompt is a clean CLI error via the
        gateway guard (``PromptSet.single`` raises ``ValueError``); a blank title
        is canonicalised to absent, so it never binds a whitespace album handle.
        """
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        prompts = self._guard.run(lambda: PromptSet.single(prompt))
        name = StartRequest.canonical_tag(title)
        album_id = self._guard.run(lambda: self._gateway_factory().new(prompts, name))
        self._formatter.emit({"album_id": album_id}, album_id)

    def get(
        self,
        album_id: Annotated[str, typer.Argument(help="Album id to copy out.")],
        dest: Annotated[
            str | None,
            typer.Option("--dest", help="Directory to export into (default: cwd)."),
        ] = None,
        *,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """Copy an album out as a directory of its parts; print the written path.

        Exports into the current directory unless ``--dest`` names another,
        matching the destination ``mic:music get`` takes.
        """
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        target_dir = dest if dest is not None else str(Path.cwd())
        target = self._guard.run(
            lambda: self._gateway_factory().get(album_id, target_dir)
        )
        self._formatter.emit({"album_id": album_id, "path": target}, target)

    def remove(
        self,
        album_id: Annotated[str, typer.Argument(help="Album id to delete.")],
        *,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """Delete a saved album by id; a playing album is refused."""
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        self._guard.run(lambda: self._gateway_factory().remove(album_id))
        self._formatter.emit({"removed": album_id}, f"removed {album_id}")
