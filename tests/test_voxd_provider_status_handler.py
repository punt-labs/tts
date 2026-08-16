"""Tests for :mod:`punt_vox.voxd.provider_status_handler`.

The handler is the daemon's answer to "what is ready and what should a
fresh repo pick?". Two request shapes fold through one code path;
credentials come from the shared :class:`ProviderCredentials` -- so
tests substitute a bespoke requirement dispatch rather than
monkey-patching os.environ, and the reply is asserted verbatim so a
drift between the wire shape and the client's decoder surfaces here.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast, final

from punt_vox.providers.credentials import ProviderCredentials
from punt_vox.voxd.provider_status_handler import ProviderStatusHandler

if TYPE_CHECKING:
    from starlette.websockets import WebSocket


def _capturing_ws() -> tuple[WebSocket, list[dict[str, object]]]:
    sent: list[dict[str, object]] = []

    @final
    class _WS:
        async def send_json(self, payload: dict[str, object]) -> None:
            sent.append(payload)

    return cast("WebSocket", _WS()), sent


class _Ready:
    """A ready :class:`CredentialRequirement` stub."""

    def satisfied(self) -> bool:
        return True

    def unmet_message(self, provider: str) -> str:
        _ = provider
        return ""


class _Unready:
    """An unready :class:`CredentialRequirement` stub."""

    def satisfied(self) -> bool:
        return False

    def unmet_message(self, provider: str) -> str:
        return f"stub: {provider} unavailable"


def _fixed_credentials() -> ProviderCredentials:
    """Return a :class:`ProviderCredentials` with a deterministic requirement map."""
    return ProviderCredentials(
        requirements={
            "elevenlabs": _Ready(),
            "openai": _Unready(),
            "polly": _Unready(),
            "say": _Ready(),
            "espeak": _Unready(),
        }
    )


class TestPerProviderRequest:
    """A request naming a provider returns one row plus the preferred name."""

    def test_reply_carries_named_provider_verdict(self) -> None:
        ws, sent = _capturing_ws()
        asyncio.run(
            ProviderStatusHandler(_fixed_credentials())(
                {"id": "p1", "provider": "openai"}, ws
            )
        )
        reply = sent[-1]
        assert reply["type"] == "provider_status"
        assert reply["id"] == "p1"
        providers = cast("list[dict[str, object]]", reply["providers"])
        assert len(providers) == 1
        assert providers[0]["name"] == "openai"
        assert providers[0]["ready"] is False
        assert providers[0]["reason"] == "no_credentials"
        # preferred rides on both request shapes so ``enable`` doesn't need
        # a second round-trip to learn what the first one already knew.
        assert reply["preferred"] == "elevenlabs"

    def test_ready_provider_reports_ok(self) -> None:
        ws, sent = _capturing_ws()
        asyncio.run(
            ProviderStatusHandler(_fixed_credentials())(
                {"id": "p2", "provider": "elevenlabs"}, ws
            )
        )
        providers = cast("list[dict[str, object]]", sent[-1]["providers"])
        assert providers[0]["ready"] is True
        assert providers[0]["reason"] == "ok"

    def test_unknown_provider_reports_the_unknown_reason(self) -> None:
        ws, sent = _capturing_ws()
        asyncio.run(
            ProviderStatusHandler(_fixed_credentials())(
                {"id": "p3", "provider": "ploly"}, ws
            )
        )
        providers = cast("list[dict[str, object]]", sent[-1]["providers"])
        assert providers[0]["reason"] == "unknown_provider"
        assert providers[0]["ready"] is False


class TestFullSet:
    """A request omitting ``provider`` returns every registered verdict."""

    def test_all_providers_in_preference_order(self) -> None:
        ws, sent = _capturing_ws()
        asyncio.run(ProviderStatusHandler(_fixed_credentials())({"id": "a1"}, ws))
        providers = cast("list[dict[str, object]]", sent[-1]["providers"])
        # The fixed order the daemon walks -- elevenlabs first (highest
        # quality cloud), platform binaries last.
        assert [row["name"] for row in providers] == [
            "elevenlabs",
            "openai",
            "polly",
            "say",
            "espeak",
        ]

    def test_preferred_rides_on_the_full_set(self) -> None:
        ws, sent = _capturing_ws()
        asyncio.run(ProviderStatusHandler(_fixed_credentials())({"id": "a2"}, ws))
        assert sent[-1]["preferred"] == "elevenlabs"

    def test_no_ready_provider_yields_null_preferred(self) -> None:
        creds = ProviderCredentials(
            requirements={
                "elevenlabs": _Unready(),
                "openai": _Unready(),
                "polly": _Unready(),
                "say": _Unready(),
                "espeak": _Unready(),
            }
        )
        ws, sent = _capturing_ws()
        asyncio.run(ProviderStatusHandler(creds)({"id": "a3"}, ws))
        assert sent[-1]["preferred"] is None


class TestMalformedFrame:
    """A malformed request rejects (id-stamped) rather than crashing the router."""

    def test_non_string_provider_field_is_a_rejection(self) -> None:
        ws, sent = _capturing_ws()
        asyncio.run(
            ProviderStatusHandler(_fixed_credentials())(
                {"id": "p9", "provider": 42}, ws
            )
        )
        reply = sent[-1]
        assert reply["type"] == "error"
        assert reply["id"] == "p9"
        assert "must be a string" in cast("str", reply["message"])
