"""The recordings-store ``mic`` verbs behind one subcommand-dispatched ``rec`` tool.

``vox <group> <subcommand>`` maps to the MCP tool ``<group>`` with its first
argument ``<subcommand>``; ``rec`` folds to the same shape as ``music``. One
:class:`RecTool` collapses the five recordings verbs behind a ``subcommand``
argument, routed through an explicit method table -- polymorphism over an
``if``-ladder (PY-OO-6). Every verb formats a JSON reply and calls exactly one
:class:`~punt_vox.client_sync.VoxClientSync` op -- the same op the ``vox rec``
CLI hits, so both surfaces share one code path and no logic is reimplemented
here.

Held apart from server.py so that module stays under the module-size and
class-count thresholds, mirroring ``server_music_tool.py``: a tool module owns
both its verbs and its own daemon-error envelope.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol, Self, final

from websockets.exceptions import WebSocketException

from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.session_spec import SessionSpec
from punt_vox.synthesis_batch import SegmentBatch
from punt_vox.types_synthesis import SynthesisSpec
from punt_vox.types_synthesis_errors import (
    ModelNotAvailableError,
    ProviderNotConfiguredError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.client_sync import VoxClientSync

__all__ = ["RecArgs", "RecSubcommand", "RecTool", "SessionDefaults"]

RecSubcommand = Literal["new", "list", "play", "get", "remove"]

# The daemon-transport faults every subcommand funnels to a JSON _error; named
# once so the whole tool shares one contract, mirroring the same tuple in
# server.py and vibe_command.py (each tool module owns its boundary handling).
_DAEMON_ERRORS = (VoxdConnectionError, VoxdProtocolError, WebSocketException, OSError)


def _error(message: str) -> str:
    """Return a JSON error string."""
    return json.dumps({"error": message})


class SessionDefaults(Protocol):
    """The session fields ``rec new`` reads to fill unset synthesis defaults.

    A structural view of :class:`~punt_vox.server.SessionConfig`; server.py
    hands in a closure yielding the live session so this module never imports
    it, keeping the dependency arrow pointing one way.
    """

    @property
    def voice(self) -> str | None:
        """Return the session voice, or None for the provider default."""

    @property
    def provider(self) -> str | None:
        """Return the session TTS provider name, or None."""

    @property
    def model(self) -> str | None:
        """Return the session TTS model name, or None."""

    @property
    def vibe_tags(self) -> str | None:
        """Return the session ElevenLabs expressive tags, or None."""

    def refresh_from_config(self) -> None:
        """Re-read the config files so the yielded defaults are current."""


@final
@dataclass(frozen=True, slots=True)
class RecArgs:
    """The raw ``rec`` tool arguments bundled for a subcommand handler.

    One frozen value object per call (PY-OO-3) instead of a fan of loose
    parameters threaded through five handlers; each handler reads only the
    fields it needs -- ``new`` the synthesis inputs, ``play``/``get``/``remove``
    the bare store ``ref``.
    """

    subcommand: str
    text: str | None = None
    voice: str | None = None
    language: str | None = None
    # Wire-shaped optional list: the multi-voice segments FastMCP delivers, or
    # absent (PY-TS-14 -- the tool schema needs the list shape).
    segments: list[dict[str, str]] | None = None
    rate: int = 90
    name: str | None = None
    stability: float | None = None
    similarity: float | None = None
    style: float | None = None
    speaker_boost: bool | None = None
    ref: str | None = None


@final
class RecTool:
    """Dispatch one ``rec`` subcommand to its recordings-store handler.

    Twin of the ``vox rec`` CLI (:class:`~punt_vox.cli_rec.RecCli`): every verb
    formats a JSON reply and calls exactly one :class:`VoxClientSync` op -- the
    same op the CLI hits. The client factory is a seam a test replaces with an
    in-memory stand-in, and the session provider yields the live synthesis
    defaults. An MCP caller is an agent, not a shell, so ``get`` returns the
    recording's bytes (base64) rather than writing a host file. The subcommand
    selects a handler through :data:`_HANDLERS`, an explicit method map -- never
    ``getattr``-by-name (PY-TS-11).
    """

    __slots__ = ("_client_factory", "_session_provider")
    _client_factory: Callable[[], VoxClientSync]
    _session_provider: Callable[[], SessionDefaults]

    def __new__(
        cls,
        client_factory: Callable[[], VoxClientSync],
        session_provider: Callable[[], SessionDefaults],
    ) -> Self:
        self = super().__new__(cls)
        self._client_factory = client_factory
        self._session_provider = session_provider
        return self

    def dispatch(
        self,
        subcommand: RecSubcommand,
        text: str | None = None,
        voice: str | None = None,
        language: str | None = None,
        segments: list[dict[str, str]] | None = None,
        rate: int = 90,
        name: str | None = None,
        stability: float | None = None,
        similarity: float | None = None,
        style: float | None = None,
        speaker_boost: bool | None = None,  # noqa: FBT001 -- MCP schema requires bool
        ref: str | None = None,
    ) -> str:
        """Author, list, play, fetch, or delete stored recordings.

        The recordings store is one daemon-owned directory of MP3s; every reply
        carries a bare store id, never a daemon path.

        Args:
            subcommand: The verb -- ``new`` synthesizes into the store; ``list``
                shows it; ``play``/``get``/``remove`` act on a bare store ``ref``.
            text: Simple text to synthesize (``new``). Ignored when segments set.
            voice: Default voice; falls back to the session voice or provider.
            language: Default ISO 639-1 language code (e.g. 'de', 'ko').
            segments: Segment objects, each with "text" and optional "voice",
                "language", and "vibe_tags".
            rate: Speech rate as a percentage. Defaults to 90.
            name: Bare filename to store under (no path). Content-addressed when
                omitted. Single-segment only.
            stability: ElevenLabs voice stability (0.0-1.0).
            similarity: ElevenLabs voice similarity boost (0.0-1.0).
            style: ElevenLabs voice style/expressiveness (0.0-1.0).
            speaker_boost: ElevenLabs speaker boost toggle.
            ref: The bare store id ``play``/``get``/``remove`` act on.

        Returns:
            JSON string: a list of ``{"id", "bytes", "cached"}`` for ``new``,
            ``{"recordings": [...]}`` for ``list``, ``{"played"}``/
            ``{"id", "bytes", "base64"}``/``{"removed"}`` for the ref verbs, and
            an ``{"error": ...}`` envelope on a bad input or daemon fault.
        """
        self._session_provider().refresh_from_config()
        args = RecArgs(
            subcommand,
            text,
            voice,
            language,
            segments,
            rate,
            name,
            stability,
            similarity,
            style,
            speaker_boost,
            ref,
        )
        handler = self._HANDLERS.get(subcommand)
        if handler is None:
            return _error(f"unknown rec subcommand: {subcommand!r}")
        return handler(self, args)

    def _new(self, args: RecArgs) -> str:
        """Synthesize speech into the store; return a bare id per segment."""
        session = self._session_provider()
        # Reject bad voice settings before any round-trip as a clean {"error"}
        # envelope (the sibling verbs' contract), not a bare exception.
        spec = SynthesisSpec(
            stability=args.stability, similarity=args.similarity, style=args.style
        )
        try:
            spec.validate()
        except ValueError as exc:
            return _error(str(exc))
        segments = args.segments
        if segments is None:
            if args.text is None:
                return _error("Provide text or segments.")
            segments = [{"text": args.text}]
        if args.name is not None and len(segments) > 1:
            return _error("name only supported for single-segment calls")
        # The daemon owns the store and is the sole authority on name validity:
        # an absent (None) name is content-addressed; an explicit name --
        # including "" -- is sent for the daemon to reject pre-ack (``is not
        # None``, not truthiness, to match the client and the CLI).
        single_name = (
            args.name if args.name is not None and len(segments) == 1 else None
        )
        client = self._client_factory()

        def _handler(seg_text: str, seg_spec: SynthesisSpec) -> dict[str, object]:
            # Bare id only -- no store path leaks to the agent (CLI parity).
            result = client.record(seg_text, seg_spec, name=single_name)
            return {
                "id": result.name,
                "bytes": result.byte_count,
                "cached": result.cached,
            }

        # Route through SessionSpec so ``rec new`` shares the one state-to-spec
        # constructor every synthesis surface uses -- state is the sole authority
        # on the provider, and an unconfigured provider is the F1 refusal rather
        # than a daemon guess. The per-call override carries the CLI-shaped
        # per-request fields; state fills provider / voice / model / vibe_tags.
        try:
            defaults = SessionSpec(session).fill(
                SynthesisSpec(
                    voice=args.voice,
                    language=args.language,
                    rate=args.rate,
                    stability=args.stability,
                    similarity=args.similarity,
                    style=args.style,
                    speaker_boost=args.speaker_boost,
                )
            )
        except (ProviderNotConfiguredError, ModelNotAvailableError) as exc:
            return _error(str(exc))
        return SegmentBatch(segments, defaults).render(
            handler=_handler, error_label="Record"
        )

    def _list(self, _args: RecArgs) -> str:
        """List the store's recordings as ``{"recordings": [{"id", "bytes"}]}``."""
        try:
            entries = self._client_factory().rec_list()
        except _DAEMON_ERRORS as exc:
            return _error(str(exc))
        rows = [{"id": e.name, "bytes": e.byte_count} for e in entries]
        return json.dumps({"recordings": rows})

    def _play(self, args: RecArgs) -> str:
        """Play recording *ref* on the daemon host; return ``{"played": ref}``."""
        if args.ref is None:
            return _error("rec play requires ref")
        try:
            self._client_factory().play(args.ref)
        except (ValueError, *_DAEMON_ERRORS) as exc:
            return _error(str(exc))
        return json.dumps({"played": args.ref})

    def _get(self, args: RecArgs) -> str:
        """Return recording *ref*'s bytes, base64-encoded, for the agent.

        The CLI ``rec get`` writes ``./<ref>`` to the caller's directory; an MCP
        caller is an agent with no such directory, so the bytes come back inline
        (base64) rather than landing on the daemon host's filesystem.
        """
        if args.ref is None:
            return _error("rec get requires ref")
        try:
            data = self._client_factory().fetch(args.ref)
        except (ValueError, *_DAEMON_ERRORS) as exc:
            return _error(str(exc))
        return json.dumps(
            {
                "id": args.ref,
                "bytes": len(data),
                "base64": base64.b64encode(data).decode(),
            }
        )

    def _remove(self, args: RecArgs) -> str:
        """Delete recording *ref* from the store; return ``{"removed": ref}``."""
        if args.ref is None:
            return _error("rec remove requires ref")
        try:
            self._client_factory().rec_remove(args.ref)
        except (ValueError, *_DAEMON_ERRORS) as exc:
            return _error(str(exc))
        return json.dumps({"removed": args.ref})

    # The subcommand -> handler map: an explicit literal of the class's own
    # methods, never getattr-by-name (PY-TS-11 forbids introspective dispatch).
    _HANDLERS: ClassVar[dict[str, Callable[[RecTool, RecArgs], str]]] = {
        "new": _new,
        "list": _list,
        "play": _play,
        "get": _get,
        "remove": _remove,
    }
