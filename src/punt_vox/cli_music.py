"""The ``vox music`` CLI -- play saved albums and author the catalog.

Playback verbs (list, play, next, status) drive a Selection on the running
Program; authoring verbs (new, get, remove) mutate the saved-album catalog.
:class:`MusicCli` is a humble object -- every read and command crosses to
``voxd`` via a gateway; the daemon owns the catalog, the CLI never touches it.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from typing import Annotated, NoReturn, Self, cast, final

import typer

from punt_vox import music_control_verb as control
from punt_vox.cli_guard import GATEWAY_ERRORS, CliGuard
from punt_vox.cli_io import OutputFlags
from punt_vox.cli_music_catalog import CatalogCli
from punt_vox.client_gateway import ClientProgramGateway
from punt_vox.client_sync import VoxClientSync
from punt_vox.config import ConfigStore
from punt_vox.dirs import DEFAULT_CONFIG_DIR, find_config_dir
from punt_vox.music_catalog_view import SavedAlbums
from punt_vox.music_state_view import MusicStateView
from punt_vox.output_formatter import OutputFormatter
from punt_vox.program_gateway import ProgramGateway
from punt_vox.types_programs.control import (
    CommandOutcome,
    SelectionRequest,
    StartRequest,
)
from punt_vox.types_programs.prompts import PromptSet

__all__ = ["MusicCli"]

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
    """The playback verbs of ``vox music``: on, stop, play, transport, and status.

    The catalog-authoring verbs live in :class:`CatalogCli`; :meth:`build_app`
    registers both into one Typer group. The split follows the seam the MCP
    surface already draws between ``MusicTool`` and ``CatalogVerbs``.
    """

    __slots__ = ("_flags", "_formatter", "_gateway_factory", "_guard", "_vibe_source")
    _formatter: OutputFormatter
    _gateway_factory: Callable[[], ProgramGateway]
    _flags: OutputFlags
    _vibe_source: Callable[[], str | None]
    _guard: CliGuard

    def __new__(
        cls,
        formatter: OutputFormatter,
        gateway_factory: Callable[[], ProgramGateway] | None = None,
        flags: OutputFlags | None = None,
        vibe_source: Callable[[], str | None] | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._formatter = formatter
        self._gateway_factory = gateway_factory or cls._default_gateway
        self._flags = flags if flags is not None else OutputFlags(formatter)
        self._vibe_source = vibe_source or cls._default_vibe
        self._guard = CliGuard(formatter)
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
    def build_app(formatter: OutputFormatter, flags: OutputFlags) -> typer.Typer:
        """Return the ``vox music`` Typer group with bound methods (no wrappers).

        The playback verbs come from :class:`MusicCli` and the authoring verbs
        from :class:`CatalogCli`; the caller sees one group either way. The verb
        set must match the MCP tool's subcommands exactly -- asserted in the
        parity tests.
        """
        cli = MusicCli(formatter, flags=flags)
        catalog = CatalogCli(formatter, flags=flags)
        app = typer.Typer(
            help="Start, play, and author background music.",
            no_args_is_help=True,
        )
        app.command("on")(cli.on)
        app.command("new")(catalog.new)
        app.command("list")(cli.list_programs)
        app.command("play")(cli.play)
        app.command("stop")(cli.stop)
        app.command("get")(catalog.get)
        app.command("remove")(catalog.remove)
        app.command("next")(cli.advance)
        app.command("prev")(cli.prev)
        app.command("pause")(cli.pause)
        app.command("resume")(cli.resume)
        app.command("status")(cli.status)
        return app

    def _command(
        self, verb: control.ControlVerb, op: Callable[[ProgramGateway], CommandOutcome]
    ) -> None:
        """Run control verb *op* against the daemon and report its outcome.

        The one body every control verb shares -- on, stop, play, and the four
        transport steps -- so a new verb is a two-line method rather than another
        copy of the guard-and-render shape.
        """
        self._report(verb, self._guard.run(lambda: op(self._gateway_factory())))

    def _report(self, verb: control.ControlVerb, outcome: CommandOutcome) -> None:
        """Emit *outcome* on both channels, as the verb reports itself."""
        self._formatter.emit(*verb.payload(outcome))

    def on(
        self,
        style: Annotated[
            str | None, typer.Option("--style", help="Style tag, e.g. 'trance'.")
        ] = None,
        title: Annotated[
            str | None, typer.Option("--title", help="Human album title.")
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
        prompts = self._guard.run(self._read_pool)
        request = StartRequest(
            style=style, vibe=self._vibe_source(), name=title, prompts=prompts
        )
        self._command(control.ON, lambda g: g.start(request))

    @staticmethod
    def _read_pool() -> PromptSet | None:
        """Return the stdin pool as a PromptSet, or None when nothing is piped.

        The ``isatty`` gate is the Unix pipeline convention: a pipe supplies the
        pool; an interactive ``vox music on`` with no pipe, or empty stdin, sends
        ``None`` so the daemon uses its minimal literal fallback. A piped payload
        is parsed and validated by :meth:`_pool_from_wire`.
        """
        if sys.stdin.isatty():
            return None
        raw = sys.stdin.read().strip()
        if not raw:
            return None
        return MusicCli._pool_from_wire(json.loads(raw))

    @staticmethod
    def _pool_from_wire(payload: object) -> PromptSet:
        """Return the piped *payload* as a PromptSet, or raise on a malformed pool.

        A piped payload must be a *complete* pool. ``from_wire`` maps an absent
        ``base_prompt`` to ``None`` -- the daemon fallback, correct for the
        no-pipe path but wrong here, where stdin clearly supplied a payload. So a
        Mapping lacking a non-empty ``base_prompt`` is a malformed pool, not a
        fallback: raise ``ValueError`` (as ``json.loads`` and ``from_wire`` also
        do) so the caller's ``_guard`` renders a clean CLI error. A non-object
        payload (list/string/number) is rejected by ``from_wire`` itself.
        """
        if not isinstance(payload, Mapping):
            pool = PromptSet.from_wire(payload)
        else:
            mapping = cast("Mapping[str, object]", payload)
            base = mapping.get("base_prompt")
            if not (isinstance(base, str) and base.strip()):
                detail = "pool must have a non-empty base_prompt and 12 variations"
                raise ValueError(detail)
            pool = PromptSet.from_wire(mapping)
        # A piped payload is never the fallback: from_wire only returns None for
        # an absent base, which the base check above already rejected.
        if pool is None:
            detail = "pool must have a non-empty base_prompt and 12 variations"
            raise ValueError(detail)
        return pool

    def list_programs(
        self,
        *,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """List catalog albums via the daemon, with their ready/total counts.

        The records go out through the one :class:`SavedAlbums` projection the
        ``music`` MCP tool reports, so neither surface can describe an album with
        a field the other omits.
        """
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        albums = SavedAlbums(self._guard.run(lambda: self._gateway_factory().catalog()))
        self._formatter.emit({"programs": albums.to_wire()}, albums.announced())

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
        per-vibe, cross-genre union radio -- both resolve. With no argument at all
        it repeats the last-played album (:meth:`_replay_last`).
        """
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        request = SelectionRequest(style=style, vibe=vibe, name=name, id=album_id)
        if request.is_empty:
            self._replay_last(request)
            return
        self._command(control.PLAY, lambda g: g.select(request))

    def _replay_last(self, request: SelectionRequest) -> None:
        """Replay the last-played album; with no history, list the catalog and fail.

        A bare ``play`` repeats the daemon's last-played album. When none has
        played yet the daemon rejects the empty request, and this prints that
        message beside the saved-album list rather than starting an arbitrary
        album, so the caller can pick one.
        """
        gateway = self._gateway_factory()
        try:
            outcome = gateway.select(request)
        except GATEWAY_ERRORS as exc:
            self._fail_with_catalog(str(exc), gateway)
        self._report(control.PLAY, outcome)

    def _fail_with_catalog(self, message: str, gateway: ProgramGateway) -> NoReturn:
        """Fail with *message* and the saved-album list, when the daemon is reachable.

        The album list is best-effort: a second daemon fault (an unreachable
        daemon, not a missing history) drops it, so the caller still sees the
        original error rather than a masking one.
        """
        try:
            albums = gateway.catalog()
        except GATEWAY_ERRORS:
            self._guard.fail(message)
        self._guard.fail(SavedAlbums(albums).appended_to(message))

    def stop(
        self,
        *,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """Stop the music program (halt playback); a no-op when already stopped.

        The one CLI halt verb, matching ``mic:music stop``: both route the same
        daemon program-stop op. A stop against an already-idle Program is
        idempotent -- the daemon acks and the CLI prints a clean confirmation.
        """
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        self._command(control.STOP, lambda g: g.stop())

    def advance(
        self,
        *,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """Step the active source forward one part (transport next)."""
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        self._command(control.STEP, lambda g: g.advance())

    def prev(
        self,
        *,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """Step the active source back one part (transport prev)."""
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        self._command(control.PREV, lambda g: g.prev())

    def pause(
        self,
        *,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """Suspend the active source in place (transport pause)."""
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        self._command(control.PAUSE, lambda g: g.pause())

    def resume(
        self,
        *,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """Continue a suspended source (transport resume)."""
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        self._command(control.RESUME, lambda g: g.resume())

    def status(
        self,
        *,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """Show the active source's authoritative status.

        Reports the same ``message``/``program``/``music_mode`` fields
        ``mic:music status`` returns, through the one :class:`MusicStateView`
        projection, so no surface can tell a caller a different music state.
        """
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        report = self._guard.run(lambda: self._gateway_factory().status())
        summary = report.summary()
        state = MusicStateView.of(report).to_dict()
        self._formatter.emit({"message": summary, **state}, summary)
