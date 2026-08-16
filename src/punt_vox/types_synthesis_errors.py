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

    Renders through :meth:`__str__` so ``str(exc)`` is the crafted sentence
    every F7 surface reports through. ``__str__`` is the load-bearing
    override: ``BaseException.__init__`` runs after ``__new__`` and
    overwrites ``args`` with the constructor's original positional arguments,
    so a message built and stashed in ``super().__new__(cls, msg)`` would
    round-trip out as the tuple repr
    ``"('eleven_v3', 'openai', ['tts-1', ...])"`` -- what six F7 surfaces on
    this branch would show if they trusted ``str(exc)``. The structured
    fields stay on ``args`` (useful to programmatic callers) while ``__str__``
    renders the message.
    """

    _model: str
    _provider: str
    _available: tuple[str, ...]

    def __new__(cls, model: str, provider: str, available: list[str]) -> Self:  # pyright: ignore[reportInconsistentConstructor]
        self = super().__new__(cls, model, provider, available)
        self._model = model
        self._provider = provider
        # Tuple-copy so the stored field is immutable outright: a caller
        # that mutates its list after raising cannot change ``str(exc)``
        # or ``exc.available``. The outbound ``list()`` copy on the
        # property alone would defend readers but not the exception's
        # own render.
        self._available = tuple(available)
        return self

    def __str__(self) -> str:
        """Render the F7 message so ``str(exc)`` is user-facing, not a tuple repr."""
        return (
            f"model {self._model!r} is not available for provider "
            f"{self._provider!r} (available: {', '.join(self._available)})"
        )

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
