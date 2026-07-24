"""The recordings-store and music-catalog ``mic`` verbs, split out of server.py.

Both are humble objects: each verb formats a JSON reply and calls exactly one
:class:`~punt_vox.client_sync.VoxClientSync` op -- the same op the ``vox`` CLI
hits, so the two surfaces share one code path and no logic is reimplemented
here. server.py wires each verb onto the ``mic`` surface, handing in a client
factory and (for the recordings store) a closure that yields the live session's
synthesis defaults. Kept apart from server.py so that module stays under the
module-size and class-count thresholds, mirroring ``vibe_command.py``: a tool
module owns both its verbs and its own daemon-error envelope.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Self, final

from websockets.exceptions import WebSocketException

from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.synthesis_batch import SegmentBatch
from punt_vox.types_synthesis import SynthesisSpec

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.client_sync import VoxClientSync

# The daemon-transport faults every tool boundary funnels to a JSON _error; named
# once so the rec/catalog verbs share one contract, mirroring the same tuple in
# server.py and vibe_command.py (each tool module owns its boundary handling).
_DAEMON_ERRORS = (VoxdConnectionError, VoxdProtocolError, WebSocketException, OSError)


def _error(message: str) -> str:
    """Return a JSON error string."""
    return json.dumps({"error": message})


class SessionDefaults(Protocol):
    """The session fields ``rec_new`` reads to fill unset synthesis defaults.

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
class RecTools:
    """The recordings-store ``mic`` verbs, as thin delegates to one engine op.

    Twin of the ``vox rec`` CLI (:class:`~punt_vox.cli_rec.RecCli`): every verb
    formats a JSON reply and calls exactly one :class:`VoxClientSync` op -- the
    same op the CLI hits, so both surfaces share one code path and no logic is
    reimplemented here. The client factory is a seam a test replaces with an
    in-memory stand-in, and the session provider yields the live synthesis
    defaults. An MCP caller is an agent, not a shell, so ``get`` returns the
    recording's bytes (base64) rather than writing a host file.
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

    def new(
        self,
        text: str | None = None,
        voice: str | None = None,
        language: str | None = None,
        segments: list[dict[str, str]] | None = None,
        rate: int = 90,
        name: str | None = None,
        stability: float | None = None,
        similarity: float | None = None,
        style: float | None = None,
        speaker_boost: bool | None = None,  # noqa: FBT001 -- MCP tool schema requires bool param
    ) -> str:
        """Synthesize speech into the store and return its bare store id.

        Pass a simple ``text`` string or a ``segments`` list. The reply carries
        the daemon-issued id only -- never a daemon path -- so the agent
        addresses the recording with ``rec_play``/``rec_get``/``rec_remove``.

        Args:
            text: Simple text to synthesize. Ignored when segments is provided.
            voice: Default voice for all segments; falls back to the session
                voice or provider default.
            language: Default ISO 639-1 language code (e.g. 'de', 'ko').
            segments: Segment objects, each with "text" and optional "voice",
                "language", and "vibe_tags".
            rate: Speech rate as a percentage. Defaults to 90.
            name: Bare filename to store under (no path). Content-addressed
                when omitted. Single-segment only.
            stability: ElevenLabs voice stability (0.0-1.0).
            similarity: ElevenLabs voice similarity boost (0.0-1.0).
            style: ElevenLabs voice style/expressiveness (0.0-1.0).
            speaker_boost: ElevenLabs speaker boost toggle.

        Returns:
            JSON string: a list of ``{"id", "bytes", "cached"}`` -- one per
            segment, the ``id`` being the bare store id.
        """
        session = self._session_provider()
        session.refresh_from_config()
        # One validation path: reject bad voice settings before any round-trip.
        spec = SynthesisSpec(stability=stability, similarity=similarity, style=style)
        spec.validate()
        if segments is None:
            if text is None:
                return _error("Provide text or segments.")
            segments = [{"text": text}]
        if name is not None and len(segments) > 1:
            return _error("name only supported for single-segment calls")
        # The daemon owns the store and is the sole authority on name validity:
        # an absent (None) name is content-addressed; an explicit name --
        # including "" -- is sent for the daemon to reject pre-ack (``is not
        # None``, not truthiness, to match the client and the CLI).
        single_name = name if name is not None and len(segments) == 1 else None
        client = self._client_factory()

        def _handler(seg_text: str, seg_spec: SynthesisSpec) -> dict[str, object]:
            # Bare id only -- no store path leaks to the agent (D-7, CLI parity).
            result = client.record(seg_text, seg_spec, name=single_name)
            return {
                "id": result.name,
                "bytes": result.byte_count,
                "cached": result.cached,
            }

        defaults = SynthesisSpec(
            voice=voice or session.voice,
            language=language,
            rate=rate,
            provider=session.provider,
            model=session.model,
            stability=stability,
            similarity=similarity,
            style=style,
            speaker_boost=speaker_boost,
            vibe_tags=session.vibe_tags,
        )
        return SegmentBatch(segments, defaults).render(
            handler=_handler, error_label="Record"
        )

    def list_recordings(self) -> str:
        """List the store's recordings as ``{"recordings": [{"id", "bytes"}]}``."""
        try:
            entries = self._client_factory().rec_list()
        except _DAEMON_ERRORS as exc:
            return _error(str(exc))
        rows = [{"id": e.name, "bytes": e.byte_count} for e in entries]
        return json.dumps({"recordings": rows})

    def play(self, ref: str) -> str:
        """Play recording *ref* on the daemon host; return ``{"played": ref}``."""
        try:
            self._client_factory().play(ref)
        except (ValueError, *_DAEMON_ERRORS) as exc:
            return _error(str(exc))
        return json.dumps({"played": ref})

    def get(self, ref: str) -> str:
        """Return recording *ref*'s bytes, base64-encoded, for the agent.

        The CLI ``rec get`` writes ``./<ref>`` to the caller's directory; an MCP
        caller is an agent with no such directory, so the bytes come back inline
        (base64) rather than landing on the daemon host's filesystem.

        Returns:
            JSON string ``{"id", "bytes", "base64"}`` -- ``bytes`` is the decoded
            length, ``base64`` the payload.
        """
        try:
            data = self._client_factory().fetch(ref)
        except (ValueError, *_DAEMON_ERRORS) as exc:
            return _error(str(exc))
        return json.dumps(
            {"id": ref, "bytes": len(data), "base64": base64.b64encode(data).decode()}
        )

    def remove(self, ref: str) -> str:
        """Delete recording *ref* from the store; return ``{"removed": ref}``."""
        try:
            self._client_factory().rec_remove(ref)
        except (ValueError, *_DAEMON_ERRORS) as exc:
            return _error(str(exc))
        return json.dumps({"removed": ref})


@final
class MusicCatalogTools:
    """The catalog-authoring ``mic`` verbs (``music_new``/``get``/``remove``).

    Twin of the ``vox music new``/``get``/``remove`` CLI verbs: each formats a
    JSON reply and calls one :class:`VoxClientSync` catalog op -- the same op the
    CLI hits. Distinct from the ``music`` on/off tool, which drives the running
    Program; these mutate the catalog and leave the active Program untouched.
    An album is a multi-part directory, so ``get`` exports it to an agent-named
    destination (the locator form) rather than returning inline bytes.
    """

    __slots__ = ("_client_factory",)
    _client_factory: Callable[[], VoxClientSync]

    def __new__(cls, client_factory: Callable[[], VoxClientSync]) -> Self:
        self = super().__new__(cls)
        self._client_factory = client_factory
        return self

    def new(self, prompt: str, name: str | None = None) -> str:
        """Generate one track into a fresh catalog album; return its bare id.

        The *prompt* is the finished ElevenLabs descriptive prompt, sent
        verbatim -- vox never expands it. Generation runs immediately (no
        confirmation) and parks the track in the catalog, leaving the active
        Program's mode, pool, and playback exactly as it found them.

        Returns:
            JSON string ``{"album_id": "<id>"}``.
        """
        try:
            album_id = self._client_factory().music_new(prompt, name)
        except (ValueError, *_DAEMON_ERRORS) as exc:
            return _error(str(exc))
        return json.dumps({"album_id": album_id})

    def get(self, album_id: str, dest: str) -> str:
        """Export album *album_id* into directory *dest*; return the locator.

        An album is a directory of parts, too large to return inline, so -- with
        no shell CWD to default to -- the agent names *dest* and vox writes
        ``<dest>/<album-name>/`` there, refusing a collision. The reply carries
        the written path, the store-locator form of the CLI ``music get``.

        Returns:
            JSON string ``{"album_id", "path"}`` -- ``path`` is the written
            album directory.
        """
        try:
            target = self._client_factory().music_get(album_id, Path(dest))
        except (ValueError, *_DAEMON_ERRORS) as exc:
            return _error(str(exc))
        return json.dumps({"album_id": album_id, "path": str(target)})

    def remove(self, album_id: str) -> str:
        """Delete catalog album *album_id* (a playing album is refused, D-2)."""
        try:
            self._client_factory().music_remove(album_id)
        except (ValueError, *_DAEMON_ERRORS) as exc:
            return _error(str(exc))
        return json.dumps({"removed": album_id})
