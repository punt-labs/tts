"""Tests for :mod:`punt_vox.types_provider`.

The wire types are the client's view of the daemon's readiness verdict;
every constructor path, every ``from_wire`` decode, and every
``to_dict`` round-trip is asserted here so a client that trusts the
Literal / dataclass never learns of an invalid frame from a distant
KeyError inside a render branch.
"""

from __future__ import annotations

import pytest

from punt_vox.types_programs import JsonObject
from punt_vox.types_provider import (
    PROVIDER_STATUS_REASONS,
    ProviderReadiness,
    ProviderStatusPayload,
)


class TestProviderStatusReasons:
    """The closed set of ``reason`` values is the source of truth for the type."""

    def test_expected_reasons_are_present(self) -> None:
        # Every reason named in the enumeration comment above the Literal
        # (types_provider.py) is a member of the runtime set.  A change to
        # the Literal must land in the frozenset in the same edit.
        assert (
            frozenset(
                {
                    "ok",
                    "unconfigured",
                    "unknown_provider",
                    "no_credentials",
                    "voxd_unavailable",
                }
            )
            == PROVIDER_STATUS_REASONS
        )


class TestProviderReadiness:
    """Construction guards the ready/reason biconditional and reason membership."""

    def test_construct_ok(self) -> None:
        row = ProviderReadiness(name="openai", ready=True, reason="ok", detail="")
        assert row.ready is True
        assert row.reason == "ok"

    def test_construct_no_credentials(self) -> None:
        row = ProviderReadiness(
            name="polly",
            ready=False,
            reason="no_credentials",
            detail="voxd has no AWS credentials",
        )
        assert row.ready is False

    def test_reject_unknown_reason(self) -> None:
        # A wire frame with a novel reason would slip past mypy (which only
        # knows the Literal); the runtime guard is the closing catch.
        with pytest.raises(ValueError, match="unknown provider_status reason"):
            ProviderReadiness(
                name="x",
                ready=False,
                reason="mystery",  # type: ignore[arg-type]  # test intent: raise
                detail="",
            )

    def test_reject_ready_true_with_nonok_reason(self) -> None:
        # ready and reason are two views of one fact.  A frame that claims
        # ready=True with reason=no_credentials is malformed on its face.
        with pytest.raises(ValueError, match="disagrees"):
            ProviderReadiness(
                name="polly", ready=True, reason="no_credentials", detail="x"
            )

    def test_reject_ready_false_with_ok_reason(self) -> None:
        with pytest.raises(ValueError, match="disagrees"):
            ProviderReadiness(name="polly", ready=False, reason="ok", detail="")

    def test_to_dict_roundtrip(self) -> None:
        original = ProviderReadiness(name="openai", ready=True, reason="ok", detail="")
        rebuilt = ProviderReadiness.from_wire(
            JsonObject.coerce(original.to_dict(), "row")
        )
        assert rebuilt == original

    def test_from_wire_rejects_missing_field(self) -> None:
        # A truncated frame surfaces as one ValueError naming the missing
        # field, not a distant KeyError far from the wire boundary.
        with pytest.raises(ValueError, match="reason"):
            ProviderReadiness.from_wire(
                JsonObject.coerce(
                    {"name": "openai", "ready": True, "detail": ""}, "row"
                )
            )

    def test_from_wire_rejects_unknown_reason(self) -> None:
        with pytest.raises(ValueError, match="unknown provider_status reason"):
            ProviderReadiness.from_wire(
                JsonObject.coerce(
                    {
                        "name": "openai",
                        "ready": False,
                        "reason": "novel",
                        "detail": "",
                    },
                    "row",
                )
            )


class TestProviderStatusPayload:
    """The wire reply parses via JsonObject and picks rows by name."""

    def test_find_returns_matching_row(self) -> None:
        row = ProviderReadiness(name="openai", ready=True, reason="ok", detail="")
        payload = ProviderStatusPayload((row,), preferred="openai")
        assert payload.find("openai") is row

    def test_find_returns_none_for_missing(self) -> None:
        payload = ProviderStatusPayload((), preferred=None)
        assert payload.find("openai") is None

    def test_from_wire_roundtrip(self) -> None:
        rows = (
            ProviderReadiness(name="openai", ready=True, reason="ok", detail=""),
            ProviderReadiness(
                name="polly",
                ready=False,
                reason="no_credentials",
                detail="voxd has no AWS credentials",
            ),
        )
        original = ProviderStatusPayload(rows, preferred="openai")
        wire = JsonObject.coerce(original.to_dict(), "provider_status")
        rebuilt = ProviderStatusPayload.from_wire(wire)
        assert rebuilt.preferred == "openai"
        assert rebuilt.providers == rows

    def test_from_wire_accepts_null_preferred(self) -> None:
        # ``preferred is None`` is the honest report of an unusable host --
        # a wire frame carrying null is a valid answer, not a parse error.
        payload = ProviderStatusPayload.from_wire(
            JsonObject.coerce({"providers": [], "preferred": None}, "provider_status")
        )
        assert payload.preferred is None
        assert payload.providers == ()
