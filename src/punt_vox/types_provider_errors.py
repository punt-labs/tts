"""Typed errors raised at the daemon's provider-resolution boundary.

Distinct from :mod:`punt_vox.types_synthesis_errors` (client-side
state errors like ``ProviderNotConfiguredError``) and from
:mod:`punt_vox.types_errors` (long-standing shared errors like
``VoiceNotFoundError`` and ``ConfigValueError``) so each module stays
under PY-OO-2's class-per-module limit and each concern lives with
its own kin. This file houses the two errors the daemon raises when
state names a provider it cannot honour: :class:`ProviderUnavailableError`
for missing credentials (F2), :class:`ProviderAuthError` for a
credential the provider rejects on the wire (F3).

Both are :class:`ValueError` subclasses so ``WireReply.reject_or_fault``
routes them through ``error()`` (message verbatim) rather than
``fault()`` (which launders every exception to ``"operation failed"``).
The design's whole promise is that a diagnosable failure crosses to
the caller unchanged; making them anything other than :class:`ValueError`
subclasses breaks that promise.
"""

from __future__ import annotations

from typing import Self

__all__ = [
    "ProviderAuthError",
    "ProviderUnavailableError",
    "UnknownProviderError",
]


class ProviderUnavailableError(ValueError):
    """Raised when state names a provider voxd has no credentials for.

    The daemon-owned failure the design's §3.5 defines: state says
    ``polly`` but voxd has no AWS credentials, state says
    ``elevenlabs`` but no ``ELEVENLABS_API_KEY``. "Your state names a
    provider this daemon cannot run" is a rejected request, not a
    daemon malfunction, so the taxonomy is already right -- the
    change is to raise this type and stop swallowing it in a broad
    ``except Exception``.

    ``__str__`` is the load-bearing override: :class:`BaseException`
    reinitialises ``args`` with the constructor's positional arguments
    after ``__new__`` runs, so a message stashed in
    ``super().__new__(cls, message)`` would round-trip out as the
    tuple repr ``"('polly', 'provider polly is ...')"``. The
    structured fields stay on ``args`` (useful to a programmatic
    caller); ``__str__`` renders the F2 sentence.
    """

    _provider_name: str
    _detail: str

    def __new__(cls, provider: str, detail: str) -> Self:  # pyright: ignore[reportInconsistentConstructor]
        self = super().__new__(cls, provider, detail)
        self._provider_name = provider
        self._detail = detail
        return self

    def __str__(self) -> str:
        """Render the F2 message so ``str(exc)`` is user-facing, not a tuple repr."""
        return self._detail

    @property
    def provider_name(self) -> str:
        """Return the provider name whose credentials were missing."""
        return self._provider_name

    @property
    def detail(self) -> str:
        """Return the caller-facing sentence naming the missing variables."""
        return self._detail


class ProviderAuthError(ValueError):
    """Raised when the provider's own credentials are rejected on the wire.

    The F3 failure. Distinct from :class:`ProviderUnavailableError`:
    credentials are *present* -- the readiness check passed -- but
    the provider rejected them at synthesis (a revoked key, an
    expired token, a wrong AWS account). Not catchable at resolution
    without a network round trip per request, so it lands here at
    the SDK boundary and crosses the wire as a rejection with the
    provider name and the HTTP status the SDK reported.

    ``__str__`` is the same load-bearing override as
    :class:`ProviderUnavailableError` and :class:`VoiceNotFoundError`,
    for the same reason: :class:`BaseException` clobbers ``args`` set
    in ``__new__``, so a message built from more than one constructor
    argument survives ``str(exc)`` only when the rendering is on
    ``__str__``, not on the tuple ``args`` fed to ``super().__new__``.
    """

    _provider_name: str
    _status_code: int | None

    def __new__(cls, provider: str, status_code: int | None = None) -> Self:  # pyright: ignore[reportInconsistentConstructor]
        self = super().__new__(cls, provider, status_code)
        self._provider_name = provider
        self._status_code = status_code
        return self

    def __str__(self) -> str:
        """Render the F3 message so ``str(exc)`` is user-facing, not a tuple repr."""
        if self._status_code is None:
            return (
                f"provider {self._provider_name!r} rejected the credentials; "
                "run `vox doctor`"
            )
        return (
            f"provider {self._provider_name!r} rejected the credentials "
            f"(HTTP {self._status_code}); run `vox doctor`"
        )

    @property
    def provider_name(self) -> str:
        """Return the provider name whose credentials were rejected."""
        return self._provider_name

    @property
    def status_code(self) -> int | None:
        """Return the SDK-reported HTTP status, or ``None`` when the SDK gave none."""
        return self._status_code


class UnknownProviderError(ValueError):
    """Raised when state names a provider the registry has no factory for.

    The F4 failure. A hand-edited ``vox.md`` naming ``provider: ploly``
    reaches the daemon (``mic:provider`` cannot produce it -- the
    ``Literal`` schema narrows it -- but the file can). Distinct from
    :class:`ProviderUnavailableError` (F2, credentials missing) and
    :class:`ProviderAuthError` (F3, credentials present but rejected):
    F4 is a lookup miss on a name the daemon does not know at all.

    Kept typed rather than plain :class:`ValueError` so the synthesize
    handler can catch it alongside the other diagnosable rejections
    without widening its net to every ``ValueError`` -- widening would
    swallow genuine daemon-side bugs (an internal invariant violation
    that happens to raise a bare ``ValueError``) and report them as
    caller-side rejections. The typed tuple is the discriminator.

    ``__str__`` is the load-bearing override for the same reason as
    :class:`ProviderUnavailableError` -- :class:`BaseException`
    reinitialises ``args`` with the constructor's positional arguments
    after ``__new__`` runs, so the message a caller sees survives only
    when it is rendered on ``__str__`` rather than stashed on the
    args tuple.
    """

    _provider_name: str
    _available: tuple[str, ...]

    def __new__(cls, provider: str, available: list[str]) -> Self:  # pyright: ignore[reportInconsistentConstructor]
        self = super().__new__(cls, provider, available)
        self._provider_name = provider
        # Tuple-copy so a caller mutating the original list after
        # raising cannot change ``str(exc)`` or ``exc.available``.
        self._available = tuple(available)
        return self

    def __str__(self) -> str:
        """Render the F4 message so ``str(exc)`` is user-facing, not a tuple repr."""
        return (
            f"Unknown provider {self._provider_name!r}. "
            f"Available: {', '.join(self._available)}"
        )

    @property
    def provider_name(self) -> str:
        """Return the provider name the registry did not know."""
        return self._provider_name

    @property
    def available(self) -> list[str]:
        """Return a copy of the registered provider names."""
        return list(self._available)
