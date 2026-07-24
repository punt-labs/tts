"""The ``vox rec`` CLI -- author, list, play, copy out, and delete recordings.

The recordings store is one daemon-owned directory of MP3s. :class:`RecCli` is a
humble object: each verb parses its arguments, calls one :class:`RecordGateway`
method, and formats the result through the shared :class:`OutputFormatter` -- no
transport detail and no daemon logic (the daemon owns the store and every path
decision). The gateway is a thin protocol so tests inject an in-memory store
instead of a live socket; production backs it with :class:`ClientRecordGateway`
over :class:`VoxClientSync`. Every id-bearing verb takes a bare store id, never a
path.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Annotated,
    NoReturn,
    Protocol,
    Self,
    final,
    runtime_checkable,
)

import typer
from websockets.exceptions import WebSocketException

from punt_vox.cli_io import TextInput
from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.client_sync import VoxClientSync
from punt_vox.output_formatter import OutputFormatter
from punt_vox.types_synthesis import SynthesisSpec

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.client import RecordingSummary, RecordResult

__all__ = ["RecCli", "build_rec_app"]

# A client error, a raw WebSocket failure (stale-token handshake / mid-request
# close, matching cli_music), or a bad name (ValueError) fails cleanly.
_GATEWAY_ERRORS = (
    VoxdConnectionError,
    VoxdProtocolError,
    WebSocketException,
    OSError,
    ValueError,
)


@runtime_checkable
class RecordGateway(Protocol):
    """The recordings-store operations a client surface issues against ``voxd``."""

    def new(self, text: str, spec: SynthesisSpec, name: str | None) -> RecordResult:
        """Synthesize *text* into the store; return the daemon's locator."""
        ...

    def recordings(self) -> tuple[RecordingSummary, ...]:
        """Return the store's recordings (name + bytes)."""
        ...

    def play(self, ref: str) -> None:
        """Play recording *ref* on the daemon host."""
        ...

    def get(self, ref: str) -> bytes:
        """Return recording *ref*'s bytes, reassembled from the chunked stream."""
        ...

    def remove(self, ref: str) -> None:
        """Delete recording *ref* from the store."""
        ...


@final
class ClientRecordGateway:
    """Back the ``RecordGateway`` seam with WebSocket calls to ``voxd``."""

    __slots__ = ("_client",)
    _client: VoxClientSync

    def __new__(cls, client: VoxClientSync) -> Self:
        self = super().__new__(cls)
        self._client = client
        return self

    def new(self, text: str, spec: SynthesisSpec, name: str | None) -> RecordResult:
        """Synthesize *text* into the store via the ``record`` op."""
        return self._client.record(text, spec, name=name)

    def recordings(self) -> tuple[RecordingSummary, ...]:
        """Return the store's recordings via the ``rec_list`` op."""
        return self._client.rec_list()

    def play(self, ref: str) -> None:
        """Play recording *ref* on the daemon host via the ``play`` op."""
        self._client.play(ref)

    def get(self, ref: str) -> bytes:
        """Return recording *ref*'s bytes via the chunked ``fetch`` op."""
        return self._client.fetch(ref)

    def remove(self, ref: str) -> None:
        """Delete recording *ref* via the ``rec_remove`` op."""
        self._client.rec_remove(ref)


# Per-verb option aliases. These are ``rec new``'s own parameters (synthesis
# flags + the bare store name), not module-global aliases shared with ``say``.
_TextArg = Annotated[
    str | None, typer.Argument(help="Text to synthesize.", show_default=False)
]
_FromOpt = Annotated[
    Path | None,
    typer.Option("--from", help="JSON file with segments array.", exists=True),
]
_VoiceOpt = Annotated[str | None, typer.Option("--voice", help="Voice name.")]
_LanguageOpt = Annotated[
    str | None, typer.Option("--language", "--lang", help="ISO 639-1 code (e.g. de).")
]
_RateOpt = Annotated[int, typer.Option("--rate", help="Speech rate percentage.")]
_NameOpt = Annotated[
    str | None,
    typer.Option(
        "--name",
        help="Bare filename to store under (no path). Default: content-addressed.",
    ),
]
_ProviderOpt = Annotated[
    str | None,
    typer.Option("--provider", envvar="TTS_PROVIDER", help="TTS provider."),
]
_ModelOpt = Annotated[
    str | None, typer.Option("--model", envvar="TTS_MODEL", help="Model name.")
]
_StabilityOpt = Annotated[
    float | None, typer.Option("--stability", help="ElevenLabs stability (0.0-1.0).")
]
_SimilarityOpt = Annotated[
    float | None, typer.Option("--similarity", help="ElevenLabs similarity (0.0-1.0).")
]
_StyleOpt = Annotated[
    float | None, typer.Option("--style", help="ElevenLabs style (0.0-1.0).")
]
_SpeakerBoostFlag = Annotated[
    bool, typer.Option("--speaker-boost", help="Enable ElevenLabs speaker boost.")
]
_RefArg = Annotated[str, typer.Argument(help="Bare store recording id.")]


@final
class RecCli:
    """The recordings-store command implementations (a humble object)."""

    __slots__ = ("_formatter", "_gateway_factory")
    _formatter: OutputFormatter
    _gateway_factory: Callable[[], RecordGateway]

    def __new__(
        cls,
        formatter: OutputFormatter,
        gateway_factory: Callable[[], RecordGateway] | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._formatter = formatter
        self._gateway_factory = gateway_factory or cls._default_gateway
        return self

    @staticmethod
    def _default_gateway() -> RecordGateway:
        """Build the production gateway -- a fresh WebSocket client per command."""
        return ClientRecordGateway(VoxClientSync())

    @staticmethod
    def _fail(message: str) -> NoReturn:
        """Print an error to stderr and exit non-zero -- a clean CLI failure."""
        typer.echo(f"Error: {message}", err=True)
        raise typer.Exit(code=1)

    def _run[T](self, op: Callable[[RecordGateway], T]) -> T:
        """Run *op* against a fresh gateway, exiting cleanly on a gateway fault."""
        try:
            return op(self._gateway_factory())
        except _GATEWAY_ERRORS as exc:
            self._fail(str(exc))

    def new(
        self,
        text: _TextArg = None,
        from_file: _FromOpt = None,
        voice: _VoiceOpt = None,
        language: _LanguageOpt = None,
        rate: _RateOpt = 90,
        name: _NameOpt = None,
        provider: _ProviderOpt = None,
        model: _ModelOpt = None,
        stability: _StabilityOpt = None,
        similarity: _SimilarityOpt = None,
        style: _StyleOpt = None,
        speaker_boost: _SpeakerBoostFlag = False,  # noqa: FBT002 -- typer bool default
    ) -> None:
        """Synthesize speech into the store and print its bare id.

        Reads text from the TEXT argument, ``--from`` (a JSON segments file), or
        stdin. Prints only the store id -- no path, no host. Act on it with
        ``rec play``/``rec get``/``rec remove``.
        """
        boost = speaker_boost if speaker_boost else None
        spec = self._validated_spec(
            SynthesisSpec(
                voice=voice,
                language=language,
                rate=rate,
                provider=provider,
                model=model,
                stability=stability,
                similarity=similarity,
                style=style,
                speaker_boost=boost,
            )
        )
        segments = TextInput(self._formatter).resolve(text, from_file)
        self._guard_name(name, segments)
        gateway = self._gateway_factory()
        for seg_text in segments:
            try:
                result = gateway.new(seg_text, spec, name)
            except _GATEWAY_ERRORS as exc:
                self._fail(str(exc))
            self._formatter.emit(
                {
                    "id": result.name,
                    "bytes": result.byte_count,
                    "cached": result.cached,
                },
                result.name,
            )

    def list_recordings(self) -> None:
        """List the store's recording ids, one per line (``--json`` for bytes)."""
        entries = self._run(lambda g: g.recordings())
        if not entries:
            self._formatter.emit({"recordings": []}, "No recordings.")
            return
        rows = [{"id": e.name, "bytes": e.byte_count} for e in entries]
        listing = "\n".join(e.name for e in entries)
        self._formatter.emit({"recordings": rows}, listing)

    def play(self, ref: _RefArg) -> None:
        """Play recording *ref* on the daemon host."""
        self._run(lambda g: g.play(ref))
        self._formatter.emit(
            {"played": ref}, f"played store recording {ref} on the daemon host"
        )

    def get(self, ref: _RefArg) -> None:
        """Copy recording *ref* into the current directory under its store name."""
        dest = Path.cwd() / ref
        if dest.exists():
            # Fast-fail the common case before the slow fetch; _land_no_clobber's
            # exclusive link is the race-free guarantee. The name is the store's,
            # not the user's choosing, so a silent overwrite is data loss (D-1).
            self._fail(f"rec get: ./{ref} exists")
        data = self._run(lambda g: g.get(ref))
        try:
            self._land_no_clobber(dest, data)
        except FileExistsError:
            self._fail(f"rec get: ./{ref} exists")  # raced in after the check
        except OSError as exc:
            self._fail(f"cannot write {dest}: {exc}")
        self._formatter.emit({"path": str(dest), "bytes": len(data)}, f"./{ref}")

    def remove(self, ref: _RefArg) -> None:
        """Delete recording *ref* from the store."""
        self._run(lambda g: g.remove(ref))
        self._formatter.emit({"removed": ref}, f"removed {ref}")

    def _validated_spec(self, spec: SynthesisSpec) -> SynthesisSpec:
        """Validate a spec at the CLI boundary, returning it for chaining."""
        try:
            spec.validate()
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        return spec

    def _guard_name(self, name: str | None, segments: list[str]) -> None:
        """Reject an empty name, or ``--name`` given for multiple segments."""
        if name is not None and not name:
            # The daemon is the single authority on name validity; the CLI
            # fast-fails an empty name before a wasted round-trip.
            self._fail("--name must not be empty")
        if name and len(segments) > 1:
            self._fail("--name supports a single segment only")

    @staticmethod
    def _land_no_clobber(dest: Path, data: bytes) -> None:
        """Write *data* to a temp sibling then hard-link it onto *dest*.

        ``os.link`` refuses to overwrite: it raises ``FileExistsError`` if
        *dest* already exists, so a name that races into place after the
        caller's absence check cannot be clobbered (D-1). A mid-write failure
        leaves no partial file -- the temp is always removed, and *dest* is the
        complete file or absent, never truncated.
        """
        fd, tmp_name = tempfile.mkstemp(dir=dest.parent, suffix=".part")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.link(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)


def build_rec_app(formatter: OutputFormatter) -> typer.Typer:
    """Return the ``vox rec`` Typer group with bound methods (no wrappers)."""
    cli = RecCli(formatter)
    app = typer.Typer(
        help="Author and manage stored recordings.",
        no_args_is_help=True,
    )
    app.command("new")(cli.new)
    app.command("list")(cli.list_recordings)
    app.command("play")(cli.play)
    app.command("get")(cli.get)
    app.command("remove")(cli.remove)
    return app
