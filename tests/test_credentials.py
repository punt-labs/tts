"""Tests for :mod:`punt_vox.providers.credentials`.

These cover the readiness object -- the "can this provider run here" answer
that replaces four partial copies scattered across the daemon, the credential
map, the desktop installer, and the ``keys.env`` writer. Every assertion is
on the full rendered message rather than a substring, because the whole
reason :class:`ProviderUnavailableError` exists is that ``BaseException``
clobbers ``args`` set in ``__new__``: a ``match="polly"`` substring test
against a tuple repr passes when the real render is broken, which is
exactly how the earlier defect (bead vox-ll26) survived review.
"""

from __future__ import annotations

import logging
import os
from unittest.mock import patch

import pytest

from punt_vox.providers.credential_requirements import (
    ApiKeyRequirement,
    AwsRequirement,
    BinaryRequirement,
)
from punt_vox.providers.credentials import (
    PROVIDER_KEY_NAMES,
    ProviderCredentials,
    ProviderReadiness,
)
from punt_vox.types_provider_errors import ProviderUnavailableError

_ELEVEN_MSG = (
    "provider 'elevenlabs' is configured but voxd has no "
    "ELEVENLABS_API_KEY; run `vox doctor`"
)
_POLLY_MSG = (
    "provider 'polly' is configured but voxd has no AWS credentials "
    "(AWS_PROFILE, or AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY); "
    "run `vox doctor`"
)
_SAY_MSG = (
    "provider 'say' is configured but say is not on voxd's PATH; run `vox doctor`"
)
_ESPEAK_MSG = (
    "provider 'espeak' is configured but neither espeak-ng nor espeak is on "
    "voxd's PATH; run `vox doctor`"
)


class TestApiKeyRequirement:
    """A single env var must be present and non-empty to satisfy the requirement."""

    def test_satisfied_with_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-something")
        assert ApiKeyRequirement("ELEVENLABS_API_KEY").satisfied() is True

    def test_unsatisfied_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        assert ApiKeyRequirement("ELEVENLABS_API_KEY").satisfied() is False

    def test_unsatisfied_when_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "")
        assert ApiKeyRequirement("OPENAI_API_KEY").satisfied() is False

    def test_unmet_message_names_variable(self) -> None:
        # Full render, never a substring: BaseException clobbers args, so the
        # assertion has to see the sentence a caller would see.
        assert (
            ApiKeyRequirement("ELEVENLABS_API_KEY").unmet_message("elevenlabs")
            == _ELEVEN_MSG
        )

    def test_unmet_message_has_no_absolute_path(self) -> None:
        # Message text crosses the wire; an absolute prefix here would leak
        # a host path, which is what SafeFault exists to prevent.
        msg = ApiKeyRequirement("OPENAI_API_KEY").unmet_message("openai")
        assert "/" not in msg


class TestAwsRequirement:
    """Polly readiness uses botocore's own chain, not the aws CLI."""

    def test_satisfied_returns_bool(self) -> None:
        # A live check -- we assert only the type, so this passes on hosts
        # with and without an AWS chain.
        assert isinstance(AwsRequirement().satisfied(), bool)

    def test_satisfied_when_boto3_reports_credentials(self) -> None:
        with patch("boto3.Session") as mock_session:
            mock_session.return_value.get_credentials.return_value = object()
            assert AwsRequirement().satisfied() is True

    def test_unsatisfied_when_boto3_returns_none(self) -> None:
        with patch("boto3.Session") as mock_session:
            mock_session.return_value.get_credentials.return_value = None
            assert AwsRequirement().satisfied() is False

    def test_unsatisfied_when_boto3_raises(self) -> None:
        # ProfileNotFound and the ClientError family all mean "no chain",
        # not "check faulted": the readiness object never becomes its own
        # fault path.
        with patch("boto3.Session", side_effect=RuntimeError("boom")):
            assert AwsRequirement().satisfied() is False

    def test_boto3_exception_is_logged_at_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The swallowed exception must leave a diagnosable record: a
        # corrupt ~/.aws config, a permissions error, or a version
        # incompatibility would otherwise reach the client as "no AWS
        # credentials" (F2) with no trace of the real cause. The log
        # names the exception TYPE (not its str) so a provider-embedded
        # newline cannot forge a second log line at this sink.
        class _CorruptConfigError(RuntimeError):
            """A stand-in for a botocore config-parse failure."""

        with (
            patch("boto3.Session", side_effect=_CorruptConfigError("bad yaml")),
            caplog.at_level(
                logging.WARNING,
                logger="punt_vox.providers.credential_requirements",
            ),
        ):
            result = AwsRequirement().satisfied()

        assert result is False
        warnings = [
            r for r in caplog.records if r.name.startswith("punt_vox.providers")
        ]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "_CorruptConfigError" in message
        assert "returning False" in message
        assert "vox doctor" in message
        assert message.splitlines() == [message]  # single physical line

    def test_unmet_message_names_variables(self) -> None:
        assert AwsRequirement().unmet_message("polly") == _POLLY_MSG


class TestBinaryRequirement:
    """Platform providers need their binary on PATH."""

    def test_satisfied_with_binary_on_path(self) -> None:
        # ``sh`` is on every POSIX host and is a stand-in for the real
        # ``say``/``espeak`` binaries.
        assert BinaryRequirement("sh").satisfied() is True

    def test_satisfied_when_any_alternative_present(self) -> None:
        assert BinaryRequirement("__no_such_binary_ever__", "sh").satisfied() is True

    def test_unsatisfied_when_absent(self) -> None:
        assert BinaryRequirement("__no_such_binary_at_all_1__").satisfied() is False

    def test_unmet_message_single_binary(self) -> None:
        assert BinaryRequirement("say").unmet_message("say") == _SAY_MSG

    def test_unmet_message_multiple_binaries(self) -> None:
        assert (
            BinaryRequirement("espeak-ng", "espeak").unmet_message("espeak")
            == _ESPEAK_MSG
        )

    def test_construction_rejects_empty_binary_list(self) -> None:
        with pytest.raises(ValueError, match="at least one binary"):
            BinaryRequirement()


class TestProviderCredentials:
    """The single object every daemon and status caller asks."""

    def test_require_passes_when_satisfied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-x")
        ProviderCredentials().require("elevenlabs")

    def test_require_raises_typed_error_with_full_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        with pytest.raises(ProviderUnavailableError) as exc_info:
            ProviderCredentials().require("elevenlabs")
        # Full-message assertion: substring would pass against a tuple
        # repr and hide the __str__ override defect.
        assert str(exc_info.value) == _ELEVEN_MSG
        assert exc_info.value.provider_name == "elevenlabs"
        assert exc_info.value.detail == _ELEVEN_MSG

    def test_require_error_is_a_valueerror(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Load-bearing: WireReply.reject_or_fault routes ValueError to
        # error() (message verbatim) rather than fault() ("operation failed").
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        with pytest.raises(ValueError):
            ProviderCredentials().require("elevenlabs")

    def test_require_passes_for_unknown_provider(self) -> None:
        # ProviderRegistry.get raises Unknown provider -- one place, one
        # verdict. Requiring here for an unknown name would be two answers.
        ProviderCredentials().require("ploly")

    def test_report_ok_when_satisfied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        readiness = ProviderCredentials().report("openai")
        assert readiness == ProviderReadiness(
            name="openai", ready=True, reason="ok", detail=""
        )

    def test_report_no_credentials_when_unmet(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        readiness = ProviderCredentials().report("elevenlabs")
        assert readiness.name == "elevenlabs"
        assert readiness.ready is False
        assert readiness.reason == "no_credentials"
        # Detail carries the whole sentence, same text require() would raise.
        assert readiness.detail == _ELEVEN_MSG

    def test_report_unknown_provider(self) -> None:
        readiness = ProviderCredentials().report("ploly")
        assert readiness == ProviderReadiness(
            name="ploly", ready=False, reason="unknown_provider", detail=""
        )

    def test_report_all_returns_every_known_provider_in_fixed_order(self) -> None:
        readiness = ProviderCredentials().report_all()
        assert tuple(r.name for r in readiness) == (
            "elevenlabs",
            "openai",
            "polly",
            "say",
            "espeak",
        )

    def test_preferred_returns_tts_provider_when_ready(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TTS_PROVIDER", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        assert ProviderCredentials().preferred() == "openai"

    def test_preferred_ignores_tts_provider_when_not_ready(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An env var that names an unready provider does not become the
        # proposal -- writing it into vox.md would produce a config that
        # refuses on first use.
        monkeypatch.setenv("TTS_PROVIDER", "elevenlabs")
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        creds = ProviderCredentials(
            requirements={
                "elevenlabs": ApiKeyRequirement("ELEVENLABS_API_KEY"),
                "openai": ApiKeyRequirement("OPENAI_API_KEY"),
                "polly": _AlwaysUnready(),
                "say": _AlwaysReady(),
                "espeak": _AlwaysUnready(),
            }
        )
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert creds.preferred() == "say"

    def test_preferred_walks_fixed_order(self) -> None:
        creds = ProviderCredentials(
            requirements={
                "elevenlabs": _AlwaysUnready(),
                "openai": _AlwaysReady(),
                "polly": _AlwaysReady(),
                "say": _AlwaysReady(),
                "espeak": _AlwaysReady(),
            }
        )
        assert creds.preferred() == "openai"

    def test_preferred_returns_none_when_nothing_ready(self) -> None:
        creds = ProviderCredentials(
            requirements={name: _AlwaysUnready() for name in _KNOWN}
        )
        assert creds.preferred() is None

    def test_providers_property_lists_the_fixed_order(self) -> None:
        assert ProviderCredentials().providers == (
            "elevenlabs",
            "openai",
            "polly",
            "say",
            "espeak",
        )


class TestProviderKeyNames:
    """The write-side of keys.env can never omit a variable the gate reads."""

    def test_includes_every_api_key_the_gate_reads(self) -> None:
        assert "ELEVENLABS_API_KEY" in PROVIDER_KEY_NAMES
        assert "OPENAI_API_KEY" in PROVIDER_KEY_NAMES

    def test_includes_aws_chain(self) -> None:
        for name in (
            "AWS_PROFILE",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_DEFAULT_REGION",
        ):
            assert name in PROVIDER_KEY_NAMES, name

    def test_tts_provider_survives_as_preference_input(self) -> None:
        assert "TTS_PROVIDER" in PROVIDER_KEY_NAMES

    def test_tts_model_is_not_a_credential(self) -> None:
        # Model is state, not a credential; it has no business in keys.env.
        assert "TTS_MODEL" not in PROVIDER_KEY_NAMES


# --- Helper doubles ---------------------------------------------------------


_KNOWN = ("elevenlabs", "openai", "polly", "say", "espeak")


class _AlwaysReady:
    """A stub requirement that reports satisfied regardless of environment."""

    def satisfied(self) -> bool:
        return True

    def unmet_message(self, provider: str) -> str:
        _ = provider  # protocol requires the parameter
        return ""


class _AlwaysUnready:
    """A stub requirement that never reports satisfied."""

    def satisfied(self) -> bool:
        return False

    def unmet_message(self, provider: str) -> str:
        return f"stub: {provider} unavailable"


def test_stubs_satisfy_the_protocol() -> None:
    """The doubles above must be usable as :class:`CredentialRequirement`.

    Round-trips both stubs through :class:`ProviderCredentials` so a
    silent Protocol-satisfaction regression (a renamed method, a
    changed return type) surfaces as a test failure rather than a
    runtime crash inside the preferred/report loops.
    """
    creds = ProviderCredentials(
        requirements={"one": _AlwaysReady(), "two": _AlwaysUnready()}
    )
    assert creds.report("one").ready is True
    assert creds.report("two").ready is False
    _ = os.environ.get("PATH")
