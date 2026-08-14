"""Tests for :class:`~punt_vox.providers.ProviderRegistry`.

The registry stopped reading state and stopped probing the environment
(design §3.3): ``get`` now requires an explicit provider name and calls
:meth:`ProviderCredentials.require` before the factory. These tests
cover that gate -- that a known-but-uncredentialed provider is a typed
:class:`ProviderUnavailableError` with the exact message, that an
unknown name still gets the pre-existing ``ValueError('Unknown ...')``,
and that a per-call ``api_key`` context is honoured because the gate
sits inside it.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from punt_vox.providers import ProviderRegistry
from punt_vox.providers.credential_requirements import ApiKeyRequirement
from punt_vox.providers.credentials import ProviderCredentials
from punt_vox.types import TTSProvider
from punt_vox.types_provider_errors import (
    ProviderUnavailableError,
    UnknownProviderError,
)


class _NoopProvider:
    """A minimal stand-in that satisfies ``TTSProvider`` for the gate tests."""

    __slots__ = ("model",)

    def __init__(self, *, model: str | None = None) -> None:
        self.model = model


def _register_test_provider(
    registry: ProviderRegistry, name: str = "elevenlabs"
) -> list[dict[str, object]]:
    """Register a factory that records its kwargs, so tests can inspect them."""
    calls: list[dict[str, object]] = []

    def _factory(**kwargs: Any) -> TTSProvider:  # pyright: ignore[reportExplicitAny, reportAny]
        calls.append(dict(kwargs))
        # cast() is honest here: the registry expects a ``TTSProvider``
        # implementation, and this test only exercises the resolution
        # gate -- no synthesize call ever reaches the double.
        return cast("TTSProvider", _NoopProvider(model=kwargs.get("model")))

    registry.register(name, _factory)
    return calls


class TestGet:
    """The resolution gate: provider required, credentials required, then factory."""

    def test_get_requires_a_provider_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A caller passing no name would be the substitution defect this
        # bead closes: the daemon has no session, so a "guess for me" call
        # cannot answer. The signature enforces the requirement at the type
        # level, and passing an empty string still raises.
        monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-x")
        registry = ProviderRegistry()
        _register_test_provider(registry)
        with pytest.raises(UnknownProviderError, match="Unknown provider ''"):
            registry.get("")

    def test_get_returns_provider_when_credentials_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-x")
        registry = ProviderRegistry()
        calls = _register_test_provider(registry)
        result = registry.get("elevenlabs")
        assert isinstance(result, _NoopProvider)
        assert calls == [{"model": None}]

    def test_get_forwards_model_kwarg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-x")
        registry = ProviderRegistry()
        calls = _register_test_provider(registry)
        registry.get("elevenlabs", model="eleven_flash_v2_5")
        assert calls == [{"model": "eleven_flash_v2_5"}]

    def test_get_lowercases_the_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-x")
        registry = ProviderRegistry()
        _register_test_provider(registry)
        # Case-insensitive resolution used to live in the ConfigStore
        # branch that's gone; the gate must still accept a mixed-case name.
        registry.get("ElevenLabs")

    def test_get_raises_provider_unavailable_when_credentials_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        registry = ProviderRegistry()
        _register_test_provider(registry)
        with pytest.raises(ProviderUnavailableError) as exc_info:
            registry.get("elevenlabs")
        # Full message assertion -- a substring pass on the tuple repr is
        # exactly the failure mode vox-ll26 documented.
        assert str(exc_info.value) == (
            "provider 'elevenlabs' is configured but voxd has no "
            "ELEVENLABS_API_KEY; run `vox doctor`"
        )
        assert exc_info.value.provider_name == "elevenlabs"

    def test_get_error_is_a_valueerror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Load-bearing: WireReply.reject_or_fault routes ValueError to
        # error() (verbatim) rather than fault() ("operation failed").
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        registry = ProviderRegistry()
        _register_test_provider(registry)
        with pytest.raises(ValueError):
            registry.get("elevenlabs")

    def test_get_raises_unknown_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # F4: a hand-edited ``vox.md`` naming a name the registry does
        # not know. Typed as :class:`UnknownProviderError` (still a
        # ``ValueError`` subclass so ``WireReply.reject_or_fault`` on
        # the voices path renders it verbatim) so the synthesize
        # handler can catch it explicitly. Full-message assertion, not
        # a substring -- the tuple-repr trap applies here too.
        monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-x")
        registry = ProviderRegistry()
        _register_test_provider(registry)
        with pytest.raises(UnknownProviderError) as exc_info:
            registry.get("ploly")
        assert str(exc_info.value) == "Unknown provider 'ploly'. Available: elevenlabs"
        assert exc_info.value.provider_name == "ploly"
        assert exc_info.value.available == ["elevenlabs"]
        # ValueError base is load-bearing for the voices-path routing
        # (system_handlers' reject_or_fault classifies ValueError as a
        # client rejection).
        assert isinstance(exc_info.value, ValueError)

    def test_get_does_not_construct_provider_when_credentials_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The whole point of gating BEFORE the factory: an uncredentialed
        # provider never reaches SDK construction, so no billable call, no
        # temp file, no cache write happens.
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        registry = ProviderRegistry()
        calls = _register_test_provider(registry)
        with pytest.raises(ProviderUnavailableError):
            registry.get("elevenlabs")
        assert calls == []

    def test_get_does_not_read_a_repo_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The ConfigStore read used to fire from ``get`` when name was
        # None; both are gone now. The signature no longer accepts a
        # config_dir at all, and the happy path proves the read is
        # absent without needing a real fixture to prove it.
        monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-x")
        registry = ProviderRegistry()
        _register_test_provider(registry)
        registry.get("elevenlabs")

    def test_get_honours_an_injected_credentials_object(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A caller supplying ``api_key=`` opens the per-call context
        # around ``get``: the check needs to see the injected value.
        # Test the plumbing by injecting a bespoke credentials object.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        creds = ProviderCredentials(
            requirements={"openai": ApiKeyRequirement("OPENAI_API_KEY")}
        )
        # Simulate the per-call context: set the key before calling get.
        monkeypatch.setenv("OPENAI_API_KEY", "per-call-key")
        registry = ProviderRegistry(credentials=creds)
        _register_test_provider(registry, name="openai")
        registry.get("openai")


class TestDefaultRegistry:
    """The module-level ``get_provider`` should wire the same gate."""

    def test_get_provider_requires_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from punt_vox.providers import get_provider

        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        with pytest.raises(ProviderUnavailableError):
            get_provider("elevenlabs")

    def test_get_provider_rejects_unknown_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from punt_vox.providers import get_provider

        monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-x")
        with pytest.raises(UnknownProviderError, match="Unknown provider 'ploly'"):
            get_provider("ploly")
