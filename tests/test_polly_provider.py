"""Tests for punt_vox.providers.polly."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from punt_vox.providers.polly import (
    PollyProvider,
    VoiceConfig,
    _bcp47_matches_iso,  # pyright: ignore[reportPrivateUsage]
    _best_engine,  # pyright: ignore[reportPrivateUsage]
    _infer_iso_from_bcp47,  # pyright: ignore[reportPrivateUsage]
)
from punt_vox.types import SynthesisRequest, VoiceNotFoundError


def _make_describe_voices_response(
    voices: list[dict[str, Any]],
) -> dict[str, Any]:
    return {"Voices": voices}


def _voice_entry(
    voice_id: str,
    language: str,
    engines: list[str],
) -> dict[str, Any]:
    return {
        "Id": voice_id,
        "LanguageCode": language,
        "SupportedEngines": engines,
    }


class TestVoiceConfig:
    def test_voice_config_is_frozen(self) -> None:
        cfg = VoiceConfig(voice_id="Joanna", language_code="en-US", engine="neural")
        with pytest.raises(AttributeError):
            cfg.voice_id = "Matthew"  # type: ignore[misc]


class TestBestEngine:
    def test_prefers_neural(self) -> None:
        assert _best_engine(["standard", "neural"]) == "neural"

    def test_prefers_neural_over_generative(self) -> None:
        assert _best_engine(["generative", "neural", "standard"]) == "neural"

    def test_falls_back_to_generative(self) -> None:
        assert _best_engine(["generative", "long-form"]) == "generative"

    def test_standard_only(self) -> None:
        assert _best_engine(["standard"]) == "standard"

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="no supported engines"):
            _best_engine([])


class TestPollyProviderResolveVoice:
    def test_resolve_cached_voice(self, polly_provider: PollyProvider) -> None:
        result = polly_provider.resolve_voice("joanna")
        assert result == "Joanna"

    def test_resolve_from_api(self) -> None:
        mock_client = MagicMock()
        mock_client.describe_voices.return_value = _make_describe_voices_response(
            [_voice_entry("Joanna", "en-US", ["neural", "standard"])]
        )
        provider = PollyProvider(boto_client=mock_client)
        result = provider.resolve_voice("joanna")
        assert result == "Joanna"

    def test_resolve_case_insensitive(self) -> None:
        mock_client = MagicMock()
        mock_client.describe_voices.return_value = _make_describe_voices_response(
            [_voice_entry("Hans", "de-DE", ["standard"])]
        )
        provider = PollyProvider(boto_client=mock_client)
        result = provider.resolve_voice("HANS")
        assert result == "Hans"

    def test_resolve_unknown_voice_raises(self) -> None:
        mock_client = MagicMock()
        mock_client.describe_voices.return_value = _make_describe_voices_response([])
        provider = PollyProvider(boto_client=mock_client)
        with pytest.raises(VoiceNotFoundError) as exc_info:
            provider.resolve_voice("nonexistent")
        assert exc_info.value.voice_name == "nonexistent"

    def test_caches_api_results(self) -> None:
        mock_client = MagicMock()
        mock_client.describe_voices.return_value = _make_describe_voices_response(
            [_voice_entry("Lucia", "es-ES", ["neural", "standard"])]
        )
        provider = PollyProvider(boto_client=mock_client)
        provider.resolve_voice("lucia")
        provider.resolve_voice("lucia")
        mock_client.describe_voices.assert_called_once()


class TestPollyProviderSynthesize:
    def test_synthesize_creates_file(
        self,
        polly_provider: PollyProvider,
        tmp_output_dir: Path,
    ) -> None:
        request = SynthesisRequest(text="hello", voice="joanna", rate=75)
        out = tmp_output_dir / "test.mp3"
        result = polly_provider.synthesize(request, out)
        assert result.path == out
        assert out.exists()
        assert out.stat().st_size > 0

    def test_synthesize_uses_ssml(
        self,
        mock_boto_client: MagicMock,
        polly_provider: PollyProvider,
        tmp_output_dir: Path,
    ) -> None:
        request = SynthesisRequest(text="Hallo", voice="hans", rate=60)
        out = tmp_output_dir / "hallo.mp3"
        polly_provider.synthesize(request, out)
        call_kwargs = mock_boto_client.synthesize_speech.call_args.kwargs
        assert call_kwargs["TextType"] == "ssml"
        assert '<prosody rate="60%">' in call_kwargs["Text"]

    def test_synthesize_passes_voice_params(
        self,
        mock_boto_client: MagicMock,
        polly_provider: PollyProvider,
        tmp_output_dir: Path,
    ) -> None:
        request = SynthesisRequest(text="Hello", voice="tatyana")
        out = tmp_output_dir / "privet.mp3"
        polly_provider.synthesize(request, out)
        call_kwargs = mock_boto_client.synthesize_speech.call_args.kwargs
        assert call_kwargs["VoiceId"] == "Tatyana"
        assert call_kwargs["LanguageCode"] == "ru-RU"
        assert call_kwargs["Engine"] == "standard"

    def test_synthesize_result_metadata(
        self,
        polly_provider: PollyProvider,
        tmp_output_dir: Path,
    ) -> None:
        request = SynthesisRequest(text="hello", voice="seoyeon")
        out = tmp_output_dir / "korean.mp3"
        result = polly_provider.synthesize(request, out)
        assert result.text == "hello"
        assert result.voice == "Seoyeon"

    def test_synthesize_translates_no_credentials_to_provider_auth_error(
        self,
        mock_boto_client: MagicMock,
        polly_provider: PollyProvider,
        tmp_output_dir: Path,
    ) -> None:
        """``NoCredentialsError`` at synthesis time crosses as F3.

        The readiness gate saw a credential chain at F2 (env, profile,
        config file) but boto3 could not resolve one by call time --
        most likely an SSO session that expired between the check and
        the synthesize call. This is a rejected credential (F3), not
        a missing configuration (F2), so it crosses through
        ``WireReply.error`` verbatim rather than the fault path.
        """
        from botocore.exceptions import NoCredentialsError

        from punt_vox.types_provider_errors import ProviderAuthError

        mock_boto_client.synthesize_speech.side_effect = NoCredentialsError()
        request = SynthesisRequest(text="hello", voice="joanna")
        with pytest.raises(ProviderAuthError) as exc_info:
            polly_provider.synthesize(request, tmp_output_dir / "auth.mp3")

        assert str(exc_info.value) == (
            "provider 'polly' rejected the credentials; run `vox doctor`"
        )
        assert exc_info.value.provider_name == "polly"
        assert exc_info.value.status_code is None

    def test_synthesize_translates_expired_token_to_provider_auth_error(
        self,
        mock_boto_client: MagicMock,
        polly_provider: PollyProvider,
        tmp_output_dir: Path,
    ) -> None:
        """An ``ExpiredToken`` ClientError crosses as F3 with HTTP 403.

        The AWS auth-family codes are the reliable discriminator --
        ExpiredToken, ExpiredTokenException, InvalidClientTokenId,
        UnrecognizedClientException all mean the credentials were
        rejected. Non-auth codes (ThrottlingException, InternalFailure)
        re-raise unchanged so the broad guard renders them opaquely --
        widening the translation would misclassify SDK/upstream faults
        as credential problems.
        """
        from botocore.exceptions import ClientError

        from punt_vox.types_provider_errors import ProviderAuthError

        mock_boto_client.synthesize_speech.side_effect = ClientError(
            # partial payload is fine at runtime; type stub is strict
            error_response=cast(
                "Any",
                {
                    "Error": {"Code": "ExpiredToken", "Message": "token expired"},
                    "ResponseMetadata": {"HTTPStatusCode": 403},
                },
            ),
            operation_name="SynthesizeSpeech",
        )
        request = SynthesisRequest(text="hello", voice="joanna")
        with pytest.raises(ProviderAuthError) as exc_info:
            polly_provider.synthesize(request, tmp_output_dir / "expired.mp3")

        assert str(exc_info.value) == (
            "provider 'polly' rejected the credentials (HTTP 403); run `vox doctor`"
        )
        assert exc_info.value.provider_name == "polly"
        assert exc_info.value.status_code == 403

    def test_synthesize_non_auth_client_error_re_raises_unchanged(
        self,
        mock_boto_client: MagicMock,
        polly_provider: PollyProvider,
        tmp_output_dir: Path,
    ) -> None:
        """A ThrottlingException is NOT an auth problem; re-raise unchanged.

        Same discipline as the ElevenLabs non-401 case: only auth-code
        errors translate to ProviderAuthError; genuine SDK/upstream
        faults keep their original exception and fall through to the
        broad guard.
        """
        from botocore.exceptions import ClientError

        mock_boto_client.synthesize_speech.side_effect = ClientError(
            # partial payload is fine at runtime; type stub is strict
            error_response=cast(
                "Any",
                {
                    "Error": {
                        "Code": "ThrottlingException",
                        "Message": "slow down",
                    },
                    "ResponseMetadata": {"HTTPStatusCode": 429},
                },
            ),
            operation_name="SynthesizeSpeech",
        )
        request = SynthesisRequest(text="hello", voice="joanna")
        with pytest.raises(ClientError) as exc_info:
            polly_provider.synthesize(request, tmp_output_dir / "throttled.mp3")
        # TypedDict access on the SDK stub is nullable; cast to plain
        # object for the assertion since we constructed the payload in
        # the test and know the shape.
        response: Any = exc_info.value.response
        assert response["Error"]["Code"] == "ThrottlingException"


class TestPollyProviderName:
    def test_name(self) -> None:
        provider = PollyProvider(boto_client=MagicMock())
        assert provider.name == "polly"


class TestPollyProviderCheckHealth:
    @patch("punt_vox.providers.polly.boto3")
    def test_all_pass(self, mock_boto3: MagicMock) -> None:
        mock_sts = MagicMock()
        mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}
        mock_polly = MagicMock()
        mock_polly.describe_voices.return_value = {"Voices": []}

        def client_factory(service: str, **_kw: object) -> MagicMock:
            if service == "sts":
                return mock_sts
            if service == "polly":
                return mock_polly
            return MagicMock()

        mock_boto3.client.side_effect = client_factory
        provider = PollyProvider(boto_client=MagicMock())
        checks = provider.check_health()
        assert len(checks) == 2
        assert all(c.passed for c in checks)

    @patch("punt_vox.providers.polly.boto3")
    def test_no_credentials(self, mock_boto3: MagicMock) -> None:
        from botocore.exceptions import NoCredentialsError

        mock_sts = MagicMock()
        mock_sts.get_caller_identity.side_effect = NoCredentialsError()

        def client_factory(service: str, **_kw: object) -> MagicMock:
            if service == "sts":
                return mock_sts
            return MagicMock()

        mock_boto3.client.side_effect = client_factory
        provider = PollyProvider(boto_client=MagicMock())
        checks = provider.check_health()
        assert not checks[0].passed
        assert "not configured" in checks[0].message


class TestBcp47MatchesIso:
    def test_standard_match(self) -> None:
        assert _bcp47_matches_iso("en-US", "en") is True

    def test_standard_no_match(self) -> None:
        assert _bcp47_matches_iso("de-DE", "en") is False

    def test_arabic_special(self) -> None:
        assert _bcp47_matches_iso("arb", "ar") is True

    def test_chinese_special(self) -> None:
        assert _bcp47_matches_iso("cmn-CN", "zh") is True

    def test_unmapped_prefix(self) -> None:
        assert _bcp47_matches_iso("en-GB", "en") is True

    def test_three_letter_no_match(self) -> None:
        assert _bcp47_matches_iso("arb", "en") is False


class TestInferIsoFromBcp47:
    def test_mapped_code(self) -> None:
        assert _infer_iso_from_bcp47("en-US") == "en"

    def test_arabic(self) -> None:
        assert _infer_iso_from_bcp47("arb") == "ar"

    def test_chinese(self) -> None:
        assert _infer_iso_from_bcp47("cmn-CN") == "zh"

    def test_unmapped_prefix(self) -> None:
        assert _infer_iso_from_bcp47("en-GB") == "en"

    def test_unmapped_three_letter(self) -> None:
        assert _infer_iso_from_bcp47("xyz") is None


class TestPollyProviderResolveVoiceWithLanguage:
    def test_matching_language(self, polly_provider: PollyProvider) -> None:
        assert polly_provider.resolve_voice("joanna", language="en") == "Joanna"

    def test_mismatching_language(self, polly_provider: PollyProvider) -> None:
        with pytest.raises(ValueError, match="does not support language 'de'"):
            polly_provider.resolve_voice("joanna", language="de")

    def test_no_language_still_works(self, polly_provider: PollyProvider) -> None:
        assert polly_provider.resolve_voice("hans") == "Hans"


class TestPollyProviderGetDefaultVoice:
    def test_known_language(self) -> None:
        provider = PollyProvider(boto_client=MagicMock())
        assert provider.get_default_voice("de") == "vicki"

    def test_english(self) -> None:
        provider = PollyProvider(boto_client=MagicMock())
        assert provider.get_default_voice("en") == "joanna"

    def test_unknown_language(self) -> None:
        provider = PollyProvider(boto_client=MagicMock())
        with pytest.raises(ValueError, match="No default voice"):
            provider.get_default_voice("xx")

    def test_case_insensitive(self) -> None:
        provider = PollyProvider(boto_client=MagicMock())
        assert provider.get_default_voice("DE") == "vicki"


class TestPollyProviderListVoices:
    def test_all_voices(self, polly_provider: PollyProvider) -> None:
        voices = polly_provider.list_voices()
        assert "joanna" in voices
        assert "hans" in voices

    def test_filter_by_language(self, polly_provider: PollyProvider) -> None:
        voices = polly_provider.list_voices(language="en")
        assert "joanna" in voices
        assert "hans" not in voices

    def test_filter_by_language_german(self, polly_provider: PollyProvider) -> None:
        voices = polly_provider.list_voices(language="de")
        assert "hans" in voices
        assert "joanna" not in voices

    def test_sorted(self, polly_provider: PollyProvider) -> None:
        voices = polly_provider.list_voices()
        assert voices == sorted(voices)


class TestPollyProviderInferLanguage:
    def test_english_voice(self, polly_provider: PollyProvider) -> None:
        assert polly_provider.infer_language_from_voice("joanna") == "en"

    def test_german_voice(self, polly_provider: PollyProvider) -> None:
        assert polly_provider.infer_language_from_voice("hans") == "de"

    def test_russian_voice(self, polly_provider: PollyProvider) -> None:
        assert polly_provider.infer_language_from_voice("tatyana") == "ru"

    def test_korean_voice(self, polly_provider: PollyProvider) -> None:
        assert polly_provider.infer_language_from_voice("seoyeon") == "ko"

    def test_unknown_voice_raises(self) -> None:
        mock_client = MagicMock()
        mock_client.describe_voices.return_value = _make_describe_voices_response([])
        provider = PollyProvider(boto_client=mock_client)
        with pytest.raises(VoiceNotFoundError) as exc_info:
            provider.infer_language_from_voice("nonexistent")
        assert exc_info.value.voice_name == "nonexistent"


class TestPollyProviderSynthesizeLanguage:
    def test_infers_language_from_voice(
        self,
        polly_provider: PollyProvider,
        tmp_output_dir: Path,
    ) -> None:
        request = SynthesisRequest(text="hello", voice="joanna")
        result = polly_provider.synthesize(request, tmp_output_dir / "test.mp3")
        assert result.language == "en"

    def test_explicit_language_preserved(
        self,
        polly_provider: PollyProvider,
        tmp_output_dir: Path,
    ) -> None:
        request = SynthesisRequest(text="hello", voice="joanna", language="en")
        result = polly_provider.synthesize(request, tmp_output_dir / "test.mp3")
        assert result.language == "en"

    def test_explicit_language_overrides_inference(
        self,
        polly_provider: PollyProvider,
        tmp_output_dir: Path,
    ) -> None:
        request = SynthesisRequest(text="Guten Tag", voice="hans", language="de")
        result = polly_provider.synthesize(request, tmp_output_dir / "test.mp3")
        assert result.language == "de"
