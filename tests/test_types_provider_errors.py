"""Tests for :mod:`punt_vox.types_provider_errors`.

Every assertion is on the FULL rendered message (never a substring),
because ``BaseException.__init__`` reinstates ``args`` after ``__new__``
and a substring match will pass against the tuple repr. That is exactly
how the earlier ``VoiceNotFoundError`` defect (bead vox-ll26) survived
review, so this suite is deliberately strict.
"""

from __future__ import annotations

import pytest

from punt_vox.types_provider_errors import (
    ProviderAuthError,
    ProviderUnavailableError,
    UnknownProviderError,
)


class TestProviderUnavailableError:
    """The daemon's F2 failure; verbatim message + typed fields."""

    def test_renders_the_detail_not_a_tuple_repr(self) -> None:
        detail = (
            "provider 'polly' is configured but voxd has no AWS credentials "
            "(AWS_PROFILE, or AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY); "
            "run `vox doctor`"
        )
        exc = ProviderUnavailableError("polly", detail)
        assert str(exc) == detail
        assert exc.provider_name == "polly"
        assert exc.detail == detail

    def test_is_a_valueerror(self) -> None:
        # Load-bearing: WireReply routes ValueError to error() (verbatim)
        # rather than fault() ("operation failed").
        exc = ProviderUnavailableError("polly", "detail")
        assert isinstance(exc, ValueError)

    def test_str_survives_reraise(self) -> None:
        # ``raise ... from ...`` runs through BaseException.__init__ again;
        # __str__ has to keep rendering the detail regardless.
        detail = "detail sentence"
        with pytest.raises(ProviderUnavailableError) as exc_info:
            try:
                raise ValueError("inner")
            except ValueError as inner:
                raise ProviderUnavailableError("polly", detail) from inner
        assert str(exc_info.value) == detail


class TestProviderAuthError:
    """The daemon's F3 failure; a network-only fact from the SDK, typed."""

    def test_renders_with_status_code(self) -> None:
        exc = ProviderAuthError("elevenlabs", 401)
        assert str(exc) == (
            "provider 'elevenlabs' rejected the credentials (HTTP 401); "
            "run `vox doctor`"
        )
        assert exc.provider_name == "elevenlabs"
        assert exc.status_code == 401

    def test_renders_without_status_code(self) -> None:
        exc = ProviderAuthError("polly")
        assert str(exc) == (
            "provider 'polly' rejected the credentials; run `vox doctor`"
        )
        assert exc.status_code is None

    def test_is_a_valueerror(self) -> None:
        assert isinstance(ProviderAuthError("openai", 401), ValueError)


class TestUnknownProviderError:
    """The F4 failure: hand-edited provider name the registry does not know."""

    def test_renders_the_sentence_not_a_tuple_repr(self) -> None:
        exc = UnknownProviderError("ploly", ["elevenlabs", "openai", "polly"])
        assert str(exc) == (
            "Unknown provider 'ploly'. Available: elevenlabs, openai, polly"
        )
        assert exc.provider_name == "ploly"
        assert exc.available == ["elevenlabs", "openai", "polly"]

    def test_is_a_valueerror(self) -> None:
        # Load-bearing on the voices path: system_handlers' reject_or_fault
        # classifies ValueError as a client rejection and sends the
        # message verbatim through WireReply.error.
        assert isinstance(UnknownProviderError("x", ["a"]), ValueError)

    def test_available_is_a_defensive_copy(self) -> None:
        # Tuple-copy in __new__ guards str(exc) too: mutating the list
        # after raise must not change what the exception renders.
        available = ["a", "b"]
        exc = UnknownProviderError("x", available)
        available.append("c")
        assert exc.available == ["a", "b"]
        assert str(exc) == "Unknown provider 'x'. Available: a, b"
