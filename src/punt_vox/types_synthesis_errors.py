"""Typed errors raised while building a :class:`SynthesisSpec` from state.

These live in their own module (rather than in :mod:`~punt_vox.types_errors`,
alongside :class:`ConfigValueError` and :class:`VoiceNotFoundError`) so
:mod:`types_errors` stays under the three-classes-per-module limit (PY-OO-2).
The distinction is more than an OO score: config errors, voice-lookup errors,
and synthesis-authority errors are three separate concerns and mixing them in
one module was already crowding the boundary.

Both errors are :class:`ValueError` subclasses so the daemon's existing
``reject_or_fault`` wire taxonomy classifies them as caller-side rejections --
the state is empty, or the state pairs a provider with a model that provider
does not offer. Client-side surfaces (CLI, MCP, hook, panel) catch them before
building a wire message; nothing crosses the boundary without an authoritative
provider name.
"""

from __future__ import annotations

from typing import Self

__all__ = [
    "ModelNotAvailableError",
    "ProviderNotConfiguredError",
]


class ProviderNotConfiguredError(ValueError):
    """Raised when state declares no TTS provider but synthesis was requested.

    Only state can name a provider -- voxd owns no session and no repo
    context, so the daemon cannot supply one. Every synthesis surface refuses
    on this before building a wire message, replacing the daemon-side
    substitution that produced the reported wrong-provider log line.
    """


class ModelNotAvailableError(ValueError):
    """Raised when state pairs a provider with a model the provider does not offer.

    Rejected rather than silently substituted for the provider's default,
    because a substitution is the class of bug this whole subsystem exists to
    prevent -- the caller must see the pair the state actually holds. Empty
    state ``model: ""`` is not this case; it means "no model declared", which
    is a permanent and legitimate state for polly, say, and espeak.
    """

    _model: str
    _provider: str
    _available: list[str]

    def __new__(cls, model: str, provider: str, available: list[str]) -> Self:  # pyright: ignore[reportInconsistentConstructor]
        message = (
            f"model {model!r} is not available for provider {provider!r} "
            f"(available: {', '.join(available)})"
        )
        self = super().__new__(cls, message)
        self._model = model
        self._provider = provider
        self._available = available
        return self

    @property
    def model(self) -> str:
        """Return the requested model name."""
        return self._model

    @property
    def provider(self) -> str:
        """Return the provider the model was requested against."""
        return self._provider

    @property
    def available(self) -> list[str]:
        """Return a copy of the provider's available model names."""
        return list(self._available)
