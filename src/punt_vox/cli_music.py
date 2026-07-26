"""The ``vox music`` CLI -- play saved albums and author the catalog.

Playback verbs (list, play, next, status) drive a Selection on the running
Program; authoring verbs (new, get, remove) mutate the saved-album catalog.
:class:`MusicCli` is a humble object -- every read and command crosses to
``voxd`` via a gateway; the daemon owns the catalog, the CLI never touches it.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, NoReturn, Self, final

import typer
from websockets.exceptions import WebSocketException

from punt_vox.catalog_gateway import CatalogGateway
from punt_vox.cli_io import OutputFlags
from punt_vox.client_catalog_gateway import ClientCatalogGateway
from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.client_gateway import ClientProgramGateway
from punt_vox.client_sync import VoxClientSync
from punt_vox.config import ConfigStore
from punt_vox.dirs import DEFAULT_CONFIG_DIR, find_config_dir
from punt_vox.output_formatter import OutputFormatter
from punt_vox.program_gateway import ProgramGateway
from punt_vox.types_programs.control import SelectionRequest, StartRequest
from punt_vox.types_programs.prompts import PromptSet
from punt_vox.types_programs.status import ProgramStatus

__all__ = ["MusicCli", "build_music_app"]

# A client error, a raw WebSocket failure (stale-token handshake / mid-request
# close, matching the MCP tools), or a bad name (ValueError) fails cleanly.
_GATEWAY_ERRORS = (
    VoxdConnectionError,
    VoxdProtocolError,
    WebSocketException,
    OSError,
    ValueError,
)

# The three output flags, redeclared on every verb so ``--json`` parses AFTER
# the subcommand (``vox music list --json``), not only before it (vox-cnak). The
# shared ``OutputFlags`` ORs both positions together (cli_io.OutputFlags.apply).
_JsonOutput = Annotated[bool, typer.Option("--json", help="Output JSON.")]
_Verbose = Annotated[
    bool, typer.Option("--verbose", "-v", help="Enable debug logging.")
]
_Quiet = Annotated[
    bool, typer.Option("--quiet", "-q", help="Suppress non-JSON output.")
]


@final
class MusicCli:
    """The music commands: playback verbs plus catalog authoring (new/get/remove)."""

    __slots__ = (
        "_catalog_factory",
        "_flags",
        "_formatter",
        "_gateway_factory",
        "_vibe_source",
    )
    _formatter: OutputFormatter
    _gateway_factory: Callable[[], ProgramGateway]
    _catalog_factory: Callable[[], CatalogGateway]
    _flags: OutputFlags
    _vibe_source: Callable[[], str | None]

    def __new__(
        cls,
        formatter: OutputFormatter,
        gateway_factory: Callable[[], ProgramGateway] | None = None,
        catalog_factory: Callable[[], CatalogGateway] | None = None,
        flags: OutputFlags | None = None,
        vibe_source: Callable[[], str | None] | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._formatter = formatter
        self._gateway_factory = gateway_factory or cls._default_gateway
        self._catalog_factory = catalog_factory or cls._default_catalog
        self._flags = flags if flags is not None else OutputFlags(formatter)
        self._vibe_source = vibe_source or cls._default_vibe
        return self

    @staticmethod
    def _default_gateway() -> ProgramGateway:
        """Build the production gateway -- a fresh WebSocket client per command."""
        return ClientProgramGateway(VoxClientSync())

    @staticmethod
    def _default_vibe() -> str | None:
        """Return the session mood from config, so ``on`` sends the same vibe.

        Reads the same ``vibe`` field the MCP tool sends from the session, so the
        CLI ``on`` and the ``music`` tool start a Program with one mood contract.
        """
        return ConfigStore(find_config_dir() or DEFAULT_CONFIG_DIR).read().vibe

    @staticmethod
    def _default_catalog() -> CatalogGateway:
        """Build the production catalog gateway -- a fresh client per command."""
        return ClientCatalogGateway(VoxClientSync())

    @staticmethod
    def _fail(message: str) -> NoReturn:
        """Print an error to stderr and exit non-zero -- a clean CLI failure."""
        typer.echo(f"Error: {message}", err=True)
        raise typer.Exit(code=1)

    def _guard[T](self, op: Callable[[], T]) -> T:
        """Run daemon call *op*, mapping any gateway fault to a clean CLI exit.

        The single place a ``voxd`` error becomes a ``typer.Exit``: every verb
        runs its gateway/catalog call through here instead of repeating the
        try/except, so the error-presentation policy lives in one method.
        """
        try:
            return op()
        except _GATEWAY_ERRORS as exc:
            self._fail(str(exc))

    def on(
        self,
        style: Annotated[
            str | None, typer.Option("--style", help="Style tag, e.g. 'trance'.")
        ] = None,
        name: Annotated[
            str | None, typer.Option("--name", help="Curated album handle to save.")
        ] = None,
        *,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """Start background music from an authored pool piped in on stdin.

        Pipe the wire-form pool JSON (``{"base_prompt": ..., "variations":
        [...]}``) on stdin: ``cat pool.json | vox music on --style trance``. With
        no pipe (an interactive shell) or empty stdin, ``prompts`` is ``None`` and
        the daemon falls back to its minimal literal prompt. A malformed pool is a
        clean CLI error via the gateway guard. The vibe comes from config, so the
        CLI and the ``music`` tool start a Program with the same mood contract.
        """
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        prompts = self._guard(self._read_pool)
        request = StartRequest(
            style=style, vibe=self._vibe_source(), name=name, prompts=prompts
        )
        outcome = self._guard(lambda: self._gateway_factory().start(request))
        self._formatter.emit(
            {"music": "on", "applied": outcome.applied},
            outcome.display("Generating music."),
        )

    @staticmethod
    def _read_pool() -> PromptSet | None:
        """Return the stdin pool as a PromptSet, or None when nothing is piped.

        The ``isatty`` gate is the Unix pipeline convention: a pipe supplies the
        pool; an interactive ``vox music on`` with no pipe, or empty stdin, sends
        ``None`` so the daemon uses its minimal literal fallback. A malformed
        pool raises ``ValueError`` (``json.loads`` / ``PromptSet.from_wire``),
        which the caller's ``_guard`` turns into a clean CLI error.
        """
        if sys.stdin.isatty():
            return None
        raw = sys.stdin.read().strip()
        if not raw:
            return None
        return PromptSet.from_wire(json.loads(raw))

    def list_programs(
        self,
        *,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """List catalog albums via the daemon, with their ready/total counts."""
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        albums = self._guard(lambda: self._gateway_factory().catalog())
        if not albums:
            self._formatter.emit({"programs": []}, "No saved albums.")
            return
        entries = [
            {
                "id": a.id,
                "style": a.style,
                "vibe": a.vibe,
                "name": a.name,
                "ready": a.ready,
                "total": a.total,
            }
            for a in albums
        ]
        listing = "\n".join(f"  {a.display_line()}" for a in albums)
        self._formatter.emit(
            {"programs": entries}, f"{len(albums)} saved album(s):\n{listing}"
        )

    def play(
        self,
        album_id: Annotated[
            str | None, typer.Argument(help="Album id or saved name to replay.")
        ] = None,
        *,
        style: Annotated[
            str | None, typer.Option("--style", help="Style tag radio, e.g. 'trance'.")
        ] = None,
        vibe: Annotated[
            str | None, typer.Option("--vibe", help="Vibe tag radio, e.g. 'calm'.")
        ] = None,
        name: Annotated[
            str | None, typer.Option("--name", help="Curated album name to replay.")
        ] = None,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """Replay an album by its bare id or name, or a tag radio by style/vibe.

        The bare positional is *id-or-name* (a saved id, else the saved-name
        radio); the ``--style``/``--vibe``/``--name`` selectors keep the shipped
        per-vibe, cross-genre union radio -- both resolve.
        """
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        request = SelectionRequest(style=style, vibe=vibe, name=name, id=album_id)
        outcome = self._guard(lambda: self._gateway_factory().select(request))
        self._formatter.emit(
            {"music": "play", "applied": outcome.applied},
            outcome.display("Playing selection."),
        )

    def new(
        self,
        prompt: Annotated[
            str, typer.Argument(help="Verbatim ElevenLabs descriptive prompt.")
        ],
        name: Annotated[
            str | None, typer.Option("--name", help="Curated album handle.")
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
        gateway guard (``PromptSet.single`` raises ``ValueError``).
        """
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        prompts = self._guard(lambda: PromptSet.single(prompt))
        album_id = self._guard(lambda: self._catalog_factory().new(prompts, name))
        self._formatter.emit({"album_id": album_id}, album_id)

    def get(
        self,
        album_id: Annotated[str, typer.Argument(help="Album id to copy out.")],
        *,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """Copy an album into the current directory as a directory of its parts."""
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        target = self._guard(
            lambda: self._catalog_factory().get(album_id, str(Path.cwd()))
        )
        self._formatter.emit({"path": target}, target)

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
        self._guard(lambda: self._catalog_factory().remove(album_id))
        self._formatter.emit({"removed": album_id}, f"removed {album_id}")

    def off(
        self,
        *,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """Turn the music program off (stop playback); a no-op when already off.

        The one CLI stop verb, matching ``mic:music mode="off"``: both route the
        same daemon program-off op. A stop against an already-idle Program is
        idempotent -- the daemon acks and the CLI prints a clean confirmation.
        """
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        outcome = self._guard(lambda: self._gateway_factory().stop())
        self._formatter.emit(
            {"music": "off", "applied": outcome.applied},
            outcome.display("Music stopped."),
        )

    def advance(
        self,
        *,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """Advance the active source to another Part."""
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        outcome = self._guard(lambda: self._gateway_factory().advance())
        self._formatter.emit(
            {"music": "next", "applied": outcome.applied},
            outcome.display("Advancing to another part."),
        )

    def status(
        self,
        *,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """Show the active source's authoritative status."""
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        report = self._guard(lambda: self._gateway_factory().status())
        self._formatter.emit(report.to_dict(), self._render_status(report))

    @staticmethod
    def _render_status(status: ProgramStatus) -> str:
        """Render a ProgramStatus as a short human block for the CLI."""
        if status.is_idle:
            return "Nothing playing."
        now = status.now_playing
        where = f"playing {now.index} of {now.of}" if now is not None else "stopped"
        head = status.name.value if status.name is not None else status.format.label
        lines = [f"{head} [{status.format.label}] — {where} ({status.mode.value})"]
        if status.generation.last_error is not None:
            lines.append(f"  error: {status.generation.last_error}")
        lines += [f"  part {f.index} failed: {f.reason}" for f in status.failed_parts]
        return "\n".join(lines)


def build_music_app(formatter: OutputFormatter, flags: OutputFlags) -> typer.Typer:
    """Return the ``vox music`` Typer group with bound methods (no wrappers)."""
    cli = MusicCli(formatter, flags=flags)
    app = typer.Typer(
        help="Start, play, and author background music.",
        no_args_is_help=True,
    )
    app.command("on")(cli.on)
    app.command("new")(cli.new)
    app.command("list")(cli.list_programs)
    app.command("play")(cli.play)
    app.command("off")(cli.off)
    app.command("get")(cli.get)
    app.command("remove")(cli.remove)
    app.command("next")(cli.advance)
    app.command("status")(cli.status)
    return app
