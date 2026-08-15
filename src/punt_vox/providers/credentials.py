"""One home for "can this provider run here".

Four partial copies used to live in ``providers/__init__.py`` (the probe),
``voxd/synthesis.py`` (``_PROVIDER_API_KEY_VAR``), ``desktop_install.py``
(``_PROVIDER_KEY_VARS``), and ``keys.py`` (``PROVIDER_KEY_NAMES``). Each
covered a different subset of the five providers, and keeping them in step
by hand was the shape of a defect. This module owns the question. Its
answers reach the daemon gate (:meth:`ProviderCredentials.require`), the
status/enable proposal path (:meth:`report_all`, :meth:`preferred`), and
the ``keys.env`` writer (:data:`PROVIDER_KEY_NAMES`) through one object.

The per-provider requirement is behaviour, not a row in a table --
an API key for two providers, an AWS credential chain for one, an
executable on ``PATH`` for two -- so it dispatches on the provider
rather than branching on its name (PY-OO-6, ``oo.md`` "polymorphism
over conditionals"). Adding a provider is one entry in the dispatch
map plus one requirement instance; nothing forks.

The concrete requirement types live in
:mod:`punt_vox.providers.credential_requirements` to keep this module
under the PY-OO-2 class-per-module limit; the Protocol they satisfy is
declared here alongside the facade that consumes them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol, Self, final, runtime_checkable

from punt_vox.providers.credential_requirements import (
    ApiKeyRequirement,
    AwsRequirement,
    BinaryRequirement,
)
from punt_vox.types_provider_errors import ProviderUnavailableError

__all__ = [
    "PROVIDER_KEY_NAMES",
    "CredentialRequirement",
    "ProviderCredentials",
    "ProviderReadiness",
]


# The fixed preference order for :meth:`ProviderCredentials.preferred`.
# elevenlabs first (highest voice quality), then openai, then polly,
# then the platform binaries. A ``TTS_PROVIDER`` env var that names a
# ready provider still wins -- this is only the fallback order.
_PREFERRED_ORDER: tuple[str, ...] = ("elevenlabs", "openai", "polly", "say", "espeak")


@runtime_checkable
class CredentialRequirement(Protocol):
    """The check one provider's credentials must pass to be considered ready.

    ``satisfied`` answers cheaply (an env-var read, a botocore chain
    probe with no network, a ``PATH`` lookup) and never raises: an
    exception here would turn a routine readiness check into a fault.
    ``unmet_message`` is the caller-facing sentence when it does not,
    naming the variable(s) or the binary but never a filesystem path
    (message text is user-visible; ``keys.env`` lives under ``$HOME``).
    """

    def satisfied(self) -> bool:
        """Return True when the credential is present for this provider."""
        ...

    def unmet_message(self, provider: str) -> str:
        """Return the message reported when the credential is missing."""
        ...


@dataclass(frozen=True, slots=True)
class ProviderReadiness:
    """A single provider's readiness verdict for the status/report path.

    ``reason`` is the closed set the design's §3.6 wire block uses:
    ``ok`` when :attr:`ready` is True, ``no_credentials`` when a known
    provider's requirement is unmet, ``unknown_provider`` when the
    name is not registered. ``detail`` is the same sentence
    :meth:`CredentialRequirement.unmet_message` would raise -- one
    text, whether the caller sees it in a status frame or a wire
    error, so status cannot drift from behaviour.
    """

    name: str
    ready: bool
    reason: str
    detail: str


@final
class ProviderCredentials:
    """The daemon-side answer to "can this provider run here".

    Owns the per-provider :class:`CredentialRequirement` dispatch and
    exposes three entry points that answer from the same code:
    :meth:`require` for the resolution gate,
    :meth:`report` / :meth:`report_all` for the status/doctor surface,
    and :meth:`preferred` for the enable proposal. Status cannot drift
    from behaviour, and enable cannot propose a provider the daemon
    would refuse: they are one function, called three ways.
    """

    __slots__ = ("_requirements",)
    _requirements: dict[str, CredentialRequirement]

    def __new__(
        cls, requirements: dict[str, CredentialRequirement] | None = None
    ) -> Self:
        self = super().__new__(cls)
        self._requirements = (
            dict(requirements) if requirements is not None else _default_requirements()
        )
        return self

    @property
    def providers(self) -> tuple[str, ...]:
        """Return the fixed preference order of known providers.

        The tuple is the same order :meth:`preferred` walks and the
        one :meth:`report_all` returns, so a caller iterating for
        display sees providers in a stable, meaningful sequence
        (highest-quality cloud first, platform binaries last).
        """
        return _PREFERRED_ORDER

    def require(self, provider: str) -> None:
        """Raise :class:`ProviderUnavailableError` when *provider* is not ready.

        The daemon's resolution gate. A provider name the daemon does
        not know still passes through -- ``ProviderRegistry.get``
        raises its own ``ValueError('Unknown provider ...')`` with the
        available names, and duplicating that verdict here would be
        two answers to one question. This method only guards the
        provider-is-known-but-uncredentialed case (F2).
        """
        requirement = self._requirements.get(provider)
        if requirement is None:
            return
        if requirement.satisfied():
            return
        raise ProviderUnavailableError(provider, requirement.unmet_message(provider))

    def report(self, provider: str) -> ProviderReadiness:
        """Return *provider*'s readiness without raising.

        The status/doctor surface's per-provider answer. An unknown
        provider name reports ``reason="unknown_provider"`` with an
        empty ``detail`` -- the caller (status or doctor) chooses how
        to render it; nothing here formats the "Available: ..." list
        that ``ProviderRegistry.get`` owns.
        """
        requirement = self._requirements.get(provider)
        if requirement is None:
            return ProviderReadiness(
                name=provider, ready=False, reason="unknown_provider", detail=""
            )
        if requirement.satisfied():
            return ProviderReadiness(name=provider, ready=True, reason="ok", detail="")
        return ProviderReadiness(
            name=provider,
            ready=False,
            reason="no_credentials",
            detail=requirement.unmet_message(provider),
        )

    def report_all(self) -> tuple[ProviderReadiness, ...]:
        """Return every known provider's verdict, in preference order."""
        return tuple(self.report(name) for name in _PREFERRED_ORDER)

    def api_key_env_vars(self) -> dict[str, str]:
        """Return ``provider -> env-var`` for every API-key-authenticated provider.

        The one place :mod:`punt_vox.voxd.synthesis`' per-call
        ``api_key`` context reaches for its provider-to-env-var map,
        so the readiness gate and the env-injection helper cannot
        list different variables for the same provider. Adding an
        API-key provider is one entry in :func:`_default_requirements`;
        this method finds it automatically.
        """
        return {
            name: req.env_var
            for name, req in self._requirements.items()
            if isinstance(req, ApiKeyRequirement)
        }

    def preferred(self) -> str | None:
        """Return the provider a fresh repo should adopt, or ``None`` if none.

        The proposal answered here reaches clients through the
        ``provider_status`` wire op (§3.6, delivered by PR 3);
        ``vox enable`` uses it in §3.8. Reading ``TTS_PROVIDER`` here
        is not a run-time override of state -- it is an input to a
        proposal a human will see written into ``vox.md``, which is
        the distinction the whole design turns on (§3.3).
        """
        # TTS_PROVIDER wins only when it names a provider that is
        # actually ready; otherwise we walk the fixed order rather
        # than proposing a value that will refuse on first use.
        env = os.environ.get("TTS_PROVIDER", "").strip().lower()
        if env and env in self._requirements and self._requirements[env].satisfied():
            return env
        for name in _PREFERRED_ORDER:
            if self._requirements[name].satisfied():
                return name
        return None


def _default_requirements() -> dict[str, CredentialRequirement]:
    """Return the fixed per-provider requirement map.

    Kept as a private module-level constructor rather than a class
    attribute so tests can substitute a bespoke dispatch by passing
    ``requirements=`` to :class:`ProviderCredentials` without
    monkey-patching module state.
    """
    return {
        "elevenlabs": ApiKeyRequirement("ELEVENLABS_API_KEY"),
        "openai": ApiKeyRequirement("OPENAI_API_KEY"),
        "polly": AwsRequirement(),
        "say": BinaryRequirement("say"),
        "espeak": BinaryRequirement("espeak-ng", "espeak"),
    }


def _api_key_env_vars() -> frozenset[str]:
    """Return every env var an :class:`ApiKeyRequirement` in the default map reads."""
    return frozenset(
        req.env_var
        for req in _default_requirements().values()
        if isinstance(req, ApiKeyRequirement)
    )


# The names ``vox daemon install`` may snapshot into ``keys.env`` and voxd
# loads at startup. Derived from the same requirement dispatch that answers
# the gate and the status probe, so ``keys.env`` cannot list a variable
# no requirement reads or omit one that does. AWS variables are enumerated
# separately (:class:`AwsRequirement` consults the whole chain rather than
# one variable, so the write-side has to know the full set boto3 can pick
# up). ``TTS_PROVIDER`` remains an input to :meth:`ProviderCredentials.preferred`
# per the operator's D2 ruling -- it can propose, never override at run time.
PROVIDER_KEY_NAMES: frozenset[str] = _api_key_env_vars() | frozenset(
    {
        "AWS_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_DEFAULT_REGION",
        "TTS_PROVIDER",
    }
)
