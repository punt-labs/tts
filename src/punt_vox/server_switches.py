"""The three switch tools: ``mic:model``, ``mic:provider``, ``mic:voice``.

One tool per concern, each with the same ``dispatch(name?)`` shape: name
absent lists what is available and marks the current selection; name present
writes to the session (and to ``.punt-labs/vox/vox.md``) and returns
``{"<concern>": "<resolved-name>"}``. The three classes hold their own
factories rather than share a base -- each owns a slightly different set of
collaborators (VoiceTool needs the daemon-facing client; ModelTool needs the
static shorthand resolver; ProviderTool needs neither), and forcing them
under one abstract base would either widen the constructor to the union of
what any concrete tool needs or push slot-empty attributes into two of them.

Held apart from ``server.py`` so that module stays under the module-size and
class-count thresholds, mirroring how ``server_music_tool.py`` and
``server_enablement.py`` are already split out. The three tools are wired
into ``mcp`` in ``server.py`` alongside ``mic:music`` and ``mic:enablement``.
"""

from __future__ import annotations

import json
import logging
import random
from typing import TYPE_CHECKING, Final, Literal, Self, final

from websockets.exceptions import WebSocketException

from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.config import ConfigStore
from punt_vox.models import MODEL_TABLE, resolve_model
from punt_vox.types_synthesis import SynthesisSpec
from punt_vox.voices import VOICE_BLURBS

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from punt_vox.client_sync import VoxClientSync
    from punt_vox.server import SessionConfig

__all__ = [
    "PROVIDER_NAMES",
    "ModelTool",
    "ProviderName",
    "ProviderTool",
    "VoiceTool",
]

logger = logging.getLogger(__name__)


# The closed provider enum (§3.2). Adding a provider is a code change, not a
# discovery-at-runtime -- so the Literal narrows the ``mic:provider`` schema
# and FastMCP rejects an unknown name before dispatch.
ProviderName = Literal["elevenlabs", "openai", "polly", "say", "espeak"]

PROVIDER_NAMES: Final[tuple[ProviderName, ...]] = (
    "elevenlabs",
    "openai",
    "polly",
    "say",
    "espeak",
)


# Non-connect faults on the voice-listing wire funnel to the same JSON error
# envelope. VoxdConnectionError (the daemon is down) is common and prosaic --
# handled separately without a stack trace -- while everything else logs
# .exception() before returning, matching how the retired ``mic:who`` tool
# reported and diagnosed the two classes.
_VOICES_FAULT_ERRORS = (VoxdProtocolError, WebSocketException, OSError, ValueError)

_FEATURED_CAP = 6


def _error(message: str) -> str:
    """Return a JSON error string -- the tools never raise across their boundary."""
    return json.dumps({"error": message})


@final
class ModelTool:
    """List or set the TTS model for the current provider (``mic:model``).

    ``dispatch(None)`` returns ``{"available", "current"}`` for the current
    session provider; ``dispatch("<name>")`` resolves shorthand via
    :func:`resolve_model`, writes to the session and to
    ``.punt-labs/vox/vox.md``, and returns ``{"model": "<full-name>"}``.
    A provider with no user-selectable model returns an ``{"error": ...}``
    envelope on either path.
    """

    __slots__ = ("_config_dir_finder", "_session_provider")
    _session_provider: Callable[[], SessionConfig]
    _config_dir_finder: Callable[[], Path | None]

    def __new__(
        cls,
        session_provider: Callable[[], SessionConfig],
        config_dir_finder: Callable[[], Path | None],
    ) -> Self:
        self = super().__new__(cls)
        self._session_provider = session_provider
        self._config_dir_finder = config_dir_finder
        return self

    def dispatch(self, name: str | None = None) -> str:
        """List available models (no arg) or set the session model (name given).

        Args:
            name: The full name (``eleven_v3``) or a shorthand (``v3``, ``flash``,
                ``turbo``, ``multilingual`` -- elevenlabs only). Absent, the
                list is returned instead.

        Returns:
            JSON string. No arg: ``{"available": [...], "current": "..."}`` for
            the current session provider (``available`` may be ``[]`` for a
            modelless provider, ``current`` may be ``null``). Name given:
            ``{"model": "<resolved-full-name>"}``. On an unknown name or a
            modelless provider: ``{"error": "..."}``.
        """
        session = self._session_provider()
        session.refresh_from_config()
        provider = session.provider or "elevenlabs"

        if name is None:
            return json.dumps(
                {
                    "available": list(MODEL_TABLE.available(provider)),
                    "current": session.model,
                }
            )

        try:
            resolved = resolve_model(name, provider)
        except ValueError as exc:
            return _error(str(exc))

        session.model = resolved
        ConfigStore(self._config_dir_finder()).write_field("model", resolved)
        return json.dumps({"model": resolved})


@final
class ProviderTool:
    """List or set the TTS provider (``mic:provider``).

    ``dispatch(None)`` returns the closed provider enum plus the current
    selection; ``dispatch("<name>")`` writes to the session and to
    ``.punt-labs/vox/vox.md``. The ``Literal`` schema (§3.2) means FastMCP
    rejects an unknown name at the tool boundary before this handler runs.
    """

    __slots__ = ("_config_dir_finder", "_session_provider")
    _session_provider: Callable[[], SessionConfig]
    _config_dir_finder: Callable[[], Path | None]

    def __new__(
        cls,
        session_provider: Callable[[], SessionConfig],
        config_dir_finder: Callable[[], Path | None],
    ) -> Self:
        self = super().__new__(cls)
        self._session_provider = session_provider
        self._config_dir_finder = config_dir_finder
        return self

    def dispatch(self, name: ProviderName | None = None) -> str:
        """List providers (no arg) or set the session provider (name given).

        Args:
            name: One of ``elevenlabs``, ``openai``, ``polly``, ``say``,
                ``espeak``. Absent, the list is returned instead.

        Returns:
            JSON string. No arg: ``{"available": [...], "current": "..."}``
            (``current`` may be ``null`` when nothing has been set yet).
            Name given: ``{"provider": "<name>"}``.
        """
        session = self._session_provider()
        session.refresh_from_config()

        if name is None:
            return json.dumps(
                {
                    "available": list(PROVIDER_NAMES),
                    "current": session.provider,
                }
            )

        session.provider = name
        ConfigStore(self._config_dir_finder()).write_field("provider", name)
        return json.dumps({"provider": name})


@final
class VoiceTool:
    """List or set the session voice (``mic:voice``).

    ``dispatch(None)`` returns the roster for the current provider, decorated
    with featured-voice blurbs; ``dispatch("<name>")`` strips a stray leading
    ``@`` sigil via :meth:`SynthesisSpec.normalize_voice`, writes to the
    session and to ``.punt-labs/vox/vox.md``, and returns
    ``{"voice": "<normalized-name>"}``. A daemon fault on the roster path
    returns ``{"error": ...}``.
    """

    __slots__ = ("_client_factory", "_config_dir_finder", "_session_provider")
    _session_provider: Callable[[], SessionConfig]
    _config_dir_finder: Callable[[], Path | None]
    _client_factory: Callable[[], VoxClientSync]

    def __new__(
        cls,
        session_provider: Callable[[], SessionConfig],
        config_dir_finder: Callable[[], Path | None],
        client_factory: Callable[[], VoxClientSync],
    ) -> Self:
        self = super().__new__(cls)
        self._session_provider = session_provider
        self._config_dir_finder = config_dir_finder
        self._client_factory = client_factory
        return self

    def dispatch(self, name: str | None = None) -> str:
        """List available voices (no arg) or set the session voice (name given).

        Args:
            name: The voice name, e.g. ``matilda``. A leading ``@`` is
                stripped. A lone ``@`` or blank is refused as an unwritable
                voice. Absent, the roster is returned instead.

        Returns:
            JSON string. No arg: ``{"provider", "current", "available",
            "featured"}`` (the current ``mic:who`` payload with ``all``
            renamed to ``available``; ``featured`` carries the blurbs).
            Name given: ``{"voice": "<normalized-name>"}``. A daemon fault on
            the roster returns ``{"error": ...}``; a blank/lone-``@`` write
            returns ``{"error": "voice name is empty"}``.
        """
        session = self._session_provider()
        session.refresh_from_config()

        if name is None:
            return self._list(session)

        normalized = SynthesisSpec.normalize_voice(name)
        if normalized is None:
            return _error("voice name is empty")

        session.voice = normalized
        ConfigStore(self._config_dir_finder()).write_field("voice", normalized)
        return json.dumps({"voice": normalized})

    def _list(self, session: SessionConfig) -> str:
        """Return the voice roster for the current provider, blurbs included."""
        client = self._client_factory()
        try:
            all_voices = client.voices(provider=session.provider)
        except VoxdConnectionError as exc:
            return _error(str(exc))
        except _VOICES_FAULT_ERRORS as exc:
            logger.exception("Voice listing failed")
            return _error(str(exc))

        provider_name = session.provider or "elevenlabs"
        featured = [
            {"name": name, "blurb": blurb}
            for (prov, name), blurb in VOICE_BLURBS.items()
            if prov == provider_name and name in all_voices
        ]

        return json.dumps(
            {
                "provider": provider_name,
                "current": session.voice,
                "available": all_voices,
                "featured": random.sample(featured, min(_FEATURED_CAP, len(featured))),
            }
        )
