"""TTS provider registry and resolution.

State on disk (or the in-memory session it seeded) is the source of truth
for which provider voxd runs. The registry no longer reads a repo config
and no longer probes the environment: it constructs the named provider
after :class:`ProviderCredentials` confirms the credentials are present,
or raises :class:`ProviderUnavailableError` with a message that names the
missing variable(s). One question, one answer, one place.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from typing import TYPE_CHECKING, Self

# elevenlabs SDK imports pydantic.v1 which warns on Python 3.14+.
# Their issue, not ours -- suppress until they ship a fix.
warnings.filterwarnings(
    "ignore",
    message="Core Pydantic V1 functionality isn't compatible with Python 3.14",
    category=UserWarning,
    module=r"elevenlabs\.core\.pydantic_utilities",
)

if TYPE_CHECKING:
    from punt_vox.providers.credentials import ProviderCredentials
    from punt_vox.types import TTSProvider

__all__ = [
    "DEFAULT_VOICES",
    "ProviderRegistry",
    "format_voice_hint",
    "get_provider",
]

# Canonical default voice per provider, used in help text.
# Must stay in sync with each provider's default_voice property.
DEFAULT_VOICES: dict[str, str] = {
    "elevenlabs": "matilda",
    "polly": "joanna",
    "openai": "nova",
    "say": "samantha",
    "espeak": "en",
}


def format_voice_hint(names: list[str], limit: int = 10) -> str:
    """Format a truncated voice list for error messages."""
    sample = names[:limit]
    hint = ", ".join(sample)
    if len(names) > limit:
        hint += f" ... ({len(names)} total)"
    return hint


class ProviderRegistry:
    """Register provider factories; resolve a named provider to an instance.

    The registry owns two things: the factory map (name -> callable that
    builds a provider) and the credentials gate that runs before every
    construction. It owns *no* session, no config-file reader, and no
    environment probe -- state is the caller's responsibility, and
    :class:`ProviderCredentials` is the only answer to "is this provider
    ready". :meth:`get` therefore requires an explicit provider name and
    trusts the caller has already opened any per-call ``api_key`` context
    the credentials check needs to see.
    """

    __slots__ = ("_credentials", "_factories")

    _factories: dict[str, Callable[..., TTSProvider]]
    _credentials: ProviderCredentials

    def __new__(
        cls,
        credentials: ProviderCredentials | None = None,
    ) -> Self:
        # Deferred import to keep credentials -> types_provider_errors ->
        # types_errors dependency chain out of this module's header, which
        # is a load-bearing entry point re-exported from ``punt_vox`` top-level.
        from punt_vox.providers.credentials import ProviderCredentials

        self = super().__new__(cls)
        self._factories = {}
        self._credentials = credentials or ProviderCredentials()
        return self

    def register(self, name: str, factory: Callable[..., TTSProvider]) -> None:
        """Register a provider factory by name."""
        self._factories[name] = factory

    def get(self, name: str, *, model: str | None = None) -> TTSProvider:
        """Return a fresh provider instance for *name*.

        Raises :class:`ProviderUnavailableError` when the daemon has no
        credentials for a known provider, and ``ValueError('Unknown
        provider ...')`` when *name* is not registered. The credentials
        check runs BEFORE the factory so an uncredentialed provider
        never reaches SDK construction (where the failure would arrive
        as whichever SDK exception the provider happens to raise, at
        whichever moment). The check must sit INSIDE any per-call
        ``api_key`` context the caller has opened around this call --
        :class:`ApiKeyRequirement` reads ``os.environ`` and a caller
        supplying ``api_key=`` deserves to be allowed through.
        """
        resolved = name.lower()
        if resolved not in self._factories:
            available = ", ".join(sorted(self._factories))
            msg = f"Unknown provider {resolved!r}. Available: {available}"
            raise ValueError(msg)
        # Gate BEFORE the factory: keep uncredentialed providers off the
        # SDK path so no billable call, no temp file, no cache entry.
        self._credentials.require(resolved)
        return self._factories[resolved](model=model)


# -- Default registry with all 5 providers --------------------------------


def _register_polly(**_kwargs: str | None) -> TTSProvider:
    from punt_vox.providers.polly import PollyProvider

    return PollyProvider()


def _register_openai(**kwargs: str | None) -> TTSProvider:
    from punt_vox.providers.openai import OpenAIProvider

    model = kwargs.get("model")
    return OpenAIProvider(model=model)


def _register_elevenlabs(**kwargs: str | None) -> TTSProvider:
    from punt_vox.providers.elevenlabs import ElevenLabsProvider

    model = kwargs.get("model")
    return ElevenLabsProvider(model=model)


def _register_say(**_kwargs: str | None) -> TTSProvider:
    from punt_vox.providers.say import SayProvider

    return SayProvider()


def _register_espeak(**_kwargs: str | None) -> TTSProvider:
    from punt_vox.providers.espeak import EspeakProvider

    return EspeakProvider()


_default_registry = ProviderRegistry()
_default_registry.register("polly", _register_polly)
_default_registry.register("openai", _register_openai)
_default_registry.register("elevenlabs", _register_elevenlabs)
_default_registry.register("say", _register_say)
_default_registry.register("espeak", _register_espeak)


def get_provider(name: str, *, model: str | None = None) -> TTSProvider:
    """Look up a provider by name.

    The module-level entry point every daemon-side call site uses.
    Provider is required -- there is no fallback, no probe, no config
    read; state is the caller's responsibility (see
    ``docs/provider-authority.md`` §3.3).
    """
    return _default_registry.get(name, model=model)
