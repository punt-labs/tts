"""AWS Polly TTS provider."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self, cast

import boto3

from punt_vox.normalize import strip_vibe_tags
from punt_vox.output import OutputResolver
from punt_vox.providers.voice_resolver import VoiceResolver
from punt_vox.types import (
    AudioProviderId,
    HealthCheck,
    SynthesisRequest,
    SynthesisResult,
    TTSProvider,
)
from punt_vox.types_provider_errors import ProviderAuthError

if TYPE_CHECKING:
    from mypy_boto3_polly.client import PollyClient as PollyClientType
    from mypy_boto3_polly.literals import (
        EngineType,
        LanguageCodeType,
        VoiceIdType,
    )

logger = logging.getLogger(__name__)

__all__ = ["PollyProvider"]

# ISO 639-1 -> Polly BCP 47 language code mapping.
_LANGUAGE_MAP: dict[str, str] = {
    "ar": "arb",
    "ca": "ca-ES",
    "cs": "cs-CZ",
    "cy": "cy-GB",
    "da": "da-DK",
    "de": "de-DE",
    "en": "en-US",
    "es": "es-ES",
    "fi": "fi-FI",
    "fr": "fr-FR",
    "hi": "hi-IN",
    "id": "id-ID",
    "it": "it-IT",
    "ja": "ja-JP",
    "ko": "ko-KR",
    "ms": "ms-MY",
    "nb": "nb-NO",
    "nl": "nl-NL",
    "pl": "pl-PL",
    "pt": "pt-BR",
    "ro": "ro-RO",
    "ru": "ru-RU",
    "sv": "sv-SE",
    "th": "th-TH",
    "tr": "tr-TR",
    "uk": "uk-UA",
    "vi": "vi-VN",
    "zh": "cmn-CN",
}

# Reverse map: BCP 47 prefix -> ISO 639-1.
_BCP47_TO_ISO: dict[str, str] = {v: k for k, v in _LANGUAGE_MAP.items()}

# Default voice per language (ISO 639-1 -> lowercase Polly voice name).
_DEFAULT_VOICES: dict[str, str] = {
    "ar": "zeina",
    "da": "naja",
    "de": "vicki",
    "en": "joanna",
    "es": "lucia",
    "fi": "suvi",
    "fr": "lea",
    "hi": "aditi",
    "it": "carla",
    "ja": "takumi",
    "ko": "seoyeon",
    "nl": "lotte",
    "nb": "liv",
    "pl": "ewa",
    "pt": "vitoria",
    "ro": "carmen",
    "ru": "tatyana",
    "sv": "astrid",
    "tr": "filiz",
    "zh": "zhiyu",
}

# Engine preference order: best quality first.
_ENGINE_PREFERENCE: list[str] = ["neural", "generative", "long-form", "standard"]


def _bcp47_matches_iso(bcp47: str, iso: str) -> bool:
    """Check if a BCP 47 language code corresponds to an ISO 639-1 code."""
    mapped = _BCP47_TO_ISO.get(bcp47)
    if mapped == iso:
        return True
    prefix = bcp47.split("-")[0]
    return len(prefix) == 2 and prefix == iso


def _infer_iso_from_bcp47(bcp47: str) -> str | None:
    """Convert a BCP 47 language code to ISO 639-1."""
    iso = _BCP47_TO_ISO.get(bcp47)
    if iso:
        return iso
    prefix = bcp47.split("-")[0]
    if len(prefix) == 2:
        return prefix
    return None


@dataclass(frozen=True)
class VoiceConfig:
    """Maps a Polly voice to its API parameters.

    Each VoiceConfig bundles a Polly voice ID with its required language
    code and engine type, eliminating the need for callers to know these
    implementation details.
    """

    voice_id: VoiceIdType
    language_code: LanguageCodeType
    engine: EngineType


def _best_engine(supported: list[str]) -> EngineType:
    """Pick the best engine from a list of supported engines."""
    if not supported:
        msg = "Voice has no supported engines"
        raise ValueError(msg)
    for engine in _ENGINE_PREFERENCE:
        if engine in supported:
            return engine  # type: ignore[return-value]
    return supported[0]  # type: ignore[return-value]


class PollyProvider(TTSProvider):
    """AWS Polly TTS provider.

    Implements the TTSProvider protocol by wrapping boto3 Polly calls.
    Accepts an optional pre-configured boto3 Polly client for
    dependency injection in tests.
    """

    if TYPE_CHECKING:
        _client: PollyClientType
    _voices: VoiceResolver[VoiceConfig]

    def __new__(cls, boto_client: PollyClientType | None = None) -> Self:
        self = super().__new__(cls)
        if boto_client is not None:
            self._client = boto_client
        else:
            self._client = cast("PollyClientType", boto3.client("polly"))  # type: ignore[redundant-cast]  # pyright: ignore[reportUnknownMemberType]

        self._voices = VoiceResolver(
            self._fetch_voices,
            default_key="joanna",
            ttl_seconds=0,
            cooldown_seconds=60,
        )
        return self

    @property
    def name(self) -> str:
        return "polly"

    @property
    def default_voice(self) -> str:
        return "joanna"

    @property
    def supports_expressive_tags(self) -> bool:
        return False

    def generate_audio(self, request: SynthesisRequest) -> SynthesisResult:
        output_path = OutputResolver.resolve(request)
        return self.synthesize(request, output_path)

    def synthesize(
        self, request: SynthesisRequest, output_path: Path
    ) -> SynthesisResult:
        """Synthesize text to an MP3 file using AWS Polly.

        Resolves the voice name to Polly parameters internally, wraps
        the text in SSML with prosody rate, and writes the MP3 output.
        """
        # Deferred so a base-only install (no botocore) keeps
        # importing polly.py cheap; matches the pattern check_health
        # already uses. Both credential-family errors surface at the
        # first call to synthesize_speech below.
        from botocore.exceptions import ClientError, NoCredentialsError

        text = strip_vibe_tags(request.text)

        resolved_voice = request.voice or self.default_voice
        voice_cfg = self._voices.resolve(resolved_voice)
        rate = request.rate if request.rate is not None else 100
        ssml_text = f'<speak><prosody rate="{rate}%">{text}</prosody></speak>'
        try:
            response = self._client.synthesize_speech(
                Text=ssml_text,
                TextType="ssml",
                VoiceId=voice_cfg.voice_id,
                LanguageCode=voice_cfg.language_code,
                OutputFormat="mp3",
                Engine=voice_cfg.engine,
                SampleRate="22050",
            )
        except NoCredentialsError as exc:
            # The readiness gate catches the credential-absent case at
            # F2; NoCredentialsError here means the gate saw a chain
            # (env, profile, config) but the SDK could not resolve one
            # by call time -- most likely an SSO session that expired
            # between the readiness check and the synthesize call.
            # Cross the wire as a rejected credential (F3) not a
            # missing one, since the caller HAS configured something.
            raise ProviderAuthError("polly", None) from exc
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {
                "InvalidSignatureException",
                "SignatureDoesNotMatch",
                "ExpiredToken",
                "ExpiredTokenException",
                "UnrecognizedClientException",
                "InvalidClientTokenId",
                "AuthFailure",
                "AccessDeniedException",
            }:
                # 400/403 for a rejected credential; the exception
                # carries its own status but the auth-family codes
                # are the reliable discriminator.
                status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                raise ProviderAuthError("polly", status) from exc
            raise

        logger.info(
            "API call: provider=polly, voice=%s, chars=%d",
            voice_cfg.voice_id,
            len(text),
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("wb") as f:
            f.write(response["AudioStream"].read())
        logger.info("Wrote %s", output_path)
        language = request.language or _infer_iso_from_bcp47(voice_cfg.language_code)
        return SynthesisResult(
            path=output_path,
            text=text,
            provider=AudioProviderId.polly,
            voice=voice_cfg.voice_id,
            language=language,
            metadata=request.metadata,
        )

    def resolve_voice(self, name: str, language: str | None = None) -> str:
        """Validate and resolve a voice name to its canonical form.

        Returns the Polly voice ID (e.g. "Joanna") if the name is valid.
        If language is provided, validates that the voice supports it.
        """
        cfg = self._voices.resolve(name)
        if language is not None and not _bcp47_matches_iso(cfg.language_code, language):
            msg = (
                f"Voice '{cfg.voice_id}' does not support language '{language}' "
                f"(supports {cfg.language_code})"
            )
            raise ValueError(msg)
        return cfg.voice_id

    def check_health(self) -> list[HealthCheck]:
        """Check AWS credentials and Polly API access."""
        from botocore.exceptions import (
            ClientError,
            EndpointConnectionError,
            NoCredentialsError,
            NoRegionError,
        )

        checks: list[HealthCheck] = []

        def _ok(msg: str) -> HealthCheck:
            return HealthCheck(passed=True, message=msg)

        def _fail(msg: str) -> HealthCheck:
            return HealthCheck(passed=False, message=msg)

        # AWS credentials (STS)
        try:
            sts: Any = boto3.client("sts")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            identity: Any = sts.get_caller_identity()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            account: str = identity["Account"]  # pyright: ignore[reportUnknownVariableType]
            checks.append(_ok(f"AWS credentials (account: {account})"))
        except NoCredentialsError:
            checks.append(
                _fail("AWS credentials: not configured (run `aws configure`)")
            )
        except NoRegionError:
            checks.append(_fail("AWS credentials: no region set (run `aws configure`)"))
        except EndpointConnectionError:
            checks.append(_fail("AWS credentials: cannot reach AWS (check network)"))
        except ClientError as e:
            checks.append(_fail(f"AWS credentials: {e}"))

        # AWS Polly access
        try:
            polly: Any = boto3.client("polly")  # pyright: ignore[reportUnknownMemberType]
            polly.describe_voices()
            checks.append(_ok("AWS Polly access"))
        except (NoCredentialsError, NoRegionError):
            checks.append(_fail("AWS Polly access: skipped (no credentials)"))
        except EndpointConnectionError:
            checks.append(_fail("AWS Polly access: cannot reach AWS (check network)"))
        except ClientError as e:
            checks.append(_fail(f"AWS Polly access: {e}"))

        return checks

    def get_default_voice(self, language: str) -> str:
        """Get the default Polly voice for a language."""
        key = language.lower()
        voice = _DEFAULT_VOICES.get(key)
        if voice is None:
            supported = ", ".join(sorted(_DEFAULT_VOICES))
            msg = f"No default voice for language '{language}'. Supported: {supported}"
            raise ValueError(msg)
        return voice

    def list_voices(self, language: str | None = None) -> list[str]:
        """List available voices, optionally filtered by language."""
        all_names = self._voices.list_all()
        if language is None:
            return all_names
        # Need the VoiceConfig to filter by language -- access the cache
        return sorted(
            name
            for name in all_names
            if _bcp47_matches_iso(self._voices.resolve(name).language_code, language)
        )

    def infer_language_from_voice(self, voice: str) -> str | None:
        """Infer ISO 639-1 language from a Polly voice name."""
        cfg = self._voices.resolve(voice)
        return _infer_iso_from_bcp47(cfg.language_code)

    def _fetch_voices(self) -> dict[str, VoiceConfig]:
        """Fetch all voices from the Polly API.

        Paginates through all pages of the describe_voices response.
        Returns a dict mapping lowercase name -> VoiceConfig.
        """
        fresh: dict[str, VoiceConfig] = {}
        next_token: str | None = None

        while True:
            kw: dict[str, Any] = {}  # pyright: ignore[reportExplicitAny]
            if next_token is not None:
                kw["NextToken"] = next_token

            resp: dict[str, Any] = self._client.describe_voices(**kw)  # type: ignore[assignment]

            for voice in resp["Voices"]:
                key = voice["Id"].lower()
                if key not in fresh:
                    fresh[key] = VoiceConfig(
                        voice_id=voice["Id"],
                        language_code=voice["LanguageCode"],
                        engine=_best_engine(voice["SupportedEngines"]),
                    )

            next_token = resp.get("NextToken")
            if not next_token:
                break

        logger.debug("Fetched %d voices from Polly API", len(fresh))
        return fresh
