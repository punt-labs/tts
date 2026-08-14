"""ElevenLabs TTS provider."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Self

from elevenlabs.core import ApiError  # pyright: ignore[reportMissingTypeStubs]

from punt_vox.normalize import strip_vibe_tags
from punt_vox.output import OutputResolver
from punt_vox.providers.chunked import chunked_synthesize
from punt_vox.providers.voice_resolver import VoiceResolver
from punt_vox.types import (
    AudioProviderId,
    HealthCheck,
    SynthesisRequest,
    SynthesisResult,
    TTSProvider,
)

logger = logging.getLogger(__name__)

__all__ = ["ElevenLabsProvider"]

# Default model. State (or an explicit ``model=`` kwarg) is the sole
# authority; the earlier ``TTS_MODEL`` env-var probe was removed to
# close the state-vs-env substitution one field over from the
# provider case (design §3.9). eleven_v3 is the highest-quality
# multilingual model and the only one that interprets bracket-style
# expressive tags ([whisper], [serious]) natively; other models
# strip the tags before synthesis. Users wanting a different model
# set it in ``vox.md``.
_DEFAULT_MODEL = "eleven_v3"

# Character limits per model (from ElevenLabs docs).
_MODEL_CHAR_LIMITS: dict[str, int] = {
    "eleven_v3": 5_000,
    "eleven_flash_v2_5": 40_000,
    "eleven_turbo_v2_5": 10_000,
    "eleven_turbo_v2": 10_000,
    "eleven_multilingual_v2": 10_000,
    "eleven_monolingual_v1": 10_000,
}
_DEFAULT_CHAR_LIMIT = 10_000

# ElevenLabs voice IDs are 20-char alphanumeric strings.
_VOICE_ID_RE = re.compile(r"^[0-9a-zA-Z]{20}$")

# Voice cache TTL in seconds -- re-fetched so newly added voices
# appear without a daemon restart.
_VOICE_CACHE_TTL_S: int = 1800


class ElevenLabsProvider(TTSProvider):
    """ElevenLabs TTS provider.

    Implements the TTSProvider protocol using the ElevenLabs SDK.
    Defaults to eleven_v3 for best voice quality. eleven_v3 interprets
    bracket-style expressive tags natively, so tags are preserved for
    that model. Other models strip tags before synthesis. A different
    model comes from ``vox.md`` (or an explicit ``model=`` kwarg),
    never from the environment: the ``TTS_MODEL`` env-var probe was
    the same substitution defect the provider gate closes, one field
    over (design §3.9).
    """

    _model: str
    _client: Any  # pyright: ignore[reportExplicitAny]
    _voices: VoiceResolver[str]

    def __new__(
        cls,
        *,
        model: str | None = None,
        client: Any | None = None,  # pyright: ignore[reportExplicitAny]
    ) -> Self:
        self = super().__new__(cls)
        self._model = model or _DEFAULT_MODEL
        if client is not None:
            self._client = client
        else:
            from elevenlabs import ElevenLabs  # pyright: ignore[reportMissingTypeStubs]

            self._client = ElevenLabs()  # pyright: ignore[reportUnknownMemberType]

        self._voices = VoiceResolver(
            self._fetch_voices,
            default_key="matilda",
            ttl_seconds=_VOICE_CACHE_TTL_S,
            cooldown_seconds=60,
        )
        return self

    @staticmethod
    def _extract_api_error_message(exc: ApiError) -> str:
        """Extract a human-readable message from an ElevenLabs ApiError."""
        body: Any = exc.body  # pyright: ignore[reportUnknownMemberType]
        if isinstance(body, dict):
            detail: Any = body.get("detail", {})  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if isinstance(detail, dict):
                msg: Any = detail.get("message", "")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                if isinstance(msg, str) and msg:
                    return msg
        return f"HTTP {exc.status_code}"

    @property
    def name(self) -> str:
        return "elevenlabs"

    @property
    def default_voice(self) -> str:
        return "matilda"

    # Models that interpret bracket-style expressive tags natively.
    # eleven_v3 treats bracket tags like [alert] and [serious] as
    # expressive cues — it does not speak them as literal words.
    # Note: the ElevenLabs Model API's `can_use_style` refers to the
    # style voice-settings slider (a float 0-1), not bracket tags.
    # eleven_v3 reports can_use_style=False yet interprets bracket
    # tags. No API property exists for bracket-tag support.
    _EXPRESSIVE_MODELS: frozenset[str] = frozenset({"eleven_v3"})

    @property
    def supports_expressive_tags(self) -> bool:
        return self._model in self._EXPRESSIVE_MODELS

    @classmethod
    def model_supports_expressive_tags(cls, model: str | None) -> bool:
        """Return whether the given model interprets expressive tags natively.

        Resolves the model the same way the constructor does (explicit
        param, else :data:`_DEFAULT_MODEL`) and checks membership in
        the expressive set. Returns True for ``eleven_v3``, which
        treats bracket tags like ``[alert]`` as expressive cues. Pure:
        does not construct the provider or touch the ElevenLabs SDK.
        The ``TTS_MODEL`` env-var branch was removed in the same
        change as the constructor's, for the same reason (design §3.9).
        """
        effective = model or _DEFAULT_MODEL
        return effective in cls._EXPRESSIVE_MODELS

    def generate_audio(self, request: SynthesisRequest) -> SynthesisResult:
        output_path = OutputResolver.resolve(request)
        return self.synthesize(request, output_path)

    def synthesize(
        self, request: SynthesisRequest, output_path: Path
    ) -> SynthesisResult:
        """Synthesize text to an MP3 file using ElevenLabs."""
        # Strip expressive tags for models that don't interpret them,
        # so the TTS engine doesn't speak "[serious]" as a literal word.
        text = request.text
        if not self.supports_expressive_tags:
            text = strip_vibe_tags(text)

        resolved_voice = request.voice or self.default_voice
        voice_id = self._resolve_voice_id(resolved_voice)
        rate = request.rate if request.rate is not None else 100

        if rate != 100:
            logger.debug(
                "ElevenLabs does not support rate adjustment (got rate=%d). "
                "Audio will be at normal speed.",
                rate,
            )

        char_limit = _MODEL_CHAR_LIMITS.get(self._model, _DEFAULT_CHAR_LIMIT)

        if len(text) > char_limit:
            chunked_synthesize(
                text=text,
                char_limit=char_limit,
                synthesize_chunk=lambda chunk, path: self._single_synthesize(
                    chunk, path, voice_id, request
                ),
                output_path=output_path,
            )
        else:
            self._single_synthesize(text, output_path, voice_id, request)

        logger.info("Wrote %s", output_path)
        display_voice = (
            resolved_voice
            if _VOICE_ID_RE.match(resolved_voice)
            else resolved_voice.lower()
        )
        return SynthesisResult(
            path=output_path,
            text=text,
            provider=AudioProviderId.elevenlabs,
            voice=display_voice,
            language=request.language,
            metadata=request.metadata,
        )

    def resolve_voice(self, name: str, language: str | None = None) -> str:  # noqa: ARG002 -- protocol requires language param
        """Validate and resolve a voice name to its canonical form.

        Language is accepted but not validated — ElevenLabs voices are
        multilingual (70+ languages with eleven_v3).
        """
        self._resolve_voice_id(name)
        if _VOICE_ID_RE.match(name):
            return name
        return name.lower()

    def check_health(self) -> list[HealthCheck]:
        """Check ElevenLabs API key and subscription status."""
        checks: list[HealthCheck] = []

        api_key = os.environ.get("ELEVENLABS_API_KEY")
        if not api_key:
            checks.append(
                HealthCheck(
                    passed=False,
                    message=("ElevenLabs API key: not set (export ELEVENLABS_API_KEY)"),
                )
            )
            return checks

        checks.append(HealthCheck(passed=True, message="ElevenLabs API key: set"))

        try:
            sub: Any = self._client.user.subscription.get()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            tier: str = sub.tier  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            used: int = sub.character_count  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            limit: int = sub.character_limit  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            checks.append(
                HealthCheck(
                    passed=True,
                    message=(
                        f"ElevenLabs subscription: {tier} ({used:,}/{limit:,} chars)"
                    ),
                )
            )
        except ApiError as exc:
            msg = self._extract_api_error_message(exc)
            checks.append(
                HealthCheck(
                    passed=False,
                    message=f"ElevenLabs subscription: {msg}",
                )
            )
        except OSError as exc:
            checks.append(
                HealthCheck(
                    passed=False,
                    message=f"ElevenLabs subscription: {exc}",
                )
            )

        return checks

    def get_default_voice(self, language: str) -> str:  # noqa: ARG002 -- protocol requires language param
        """Get the default ElevenLabs voice for a language.

        ElevenLabs voices are multilingual; always returns 'matilda'.
        """
        return self.default_voice

    def list_voices(self, language: str | None = None) -> list[str]:  # noqa: ARG002 -- protocol requires language param
        """List available voices.

        ElevenLabs voices are multilingual; language filter is accepted
        but all voices are returned regardless. Only short names (without
        descriptions) are included.
        """
        return [k for k in self._voices.list_all() if " - " not in k]

    def infer_language_from_voice(self, voice: str) -> str | None:  # noqa: ARG002 -- protocol requires voice param
        """Infer language from a voice name.

        ElevenLabs voices are multilingual; always returns None.
        """
        return None

    # -- Private helpers --------------------------------------------------

    def _fetch_voices(self) -> dict[str, str]:
        """Fetch all voices from the ElevenLabs API.

        Returns a dict mapping lowercase name -> voice_id. Both the
        full descriptive name (e.g. "adam - dominant, firm") and the
        short name (e.g. "adam") are included as separate keys.
        """
        fresh: dict[str, str] = {}

        response: Any = self._client.voices.get_all()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        for voice in response.voices:  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            full_name: str = voice.name.lower()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            vid: str = voice.voice_id  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

            # Store full name (e.g. "adam - dominant, firm").
            if full_name not in fresh:
                fresh[full_name] = vid

            # Also store short name (before " - ") for convenient lookup.
            short_name = full_name.split(" - ", 1)[0]
            if short_name != full_name and short_name not in fresh:
                fresh[short_name] = vid

        logger.debug(
            "Fetched %d voice entries from ElevenLabs API",
            len(fresh),
        )
        return fresh

    def _resolve_voice_id(self, name: str) -> str:
        """Resolve a voice name or ID to a voice_id string."""
        # Accept raw voice_id (20 alphanumeric chars) directly.
        if _VOICE_ID_RE.match(name):
            return name

        return self._voices.resolve(name)

    def _build_voice_settings(self, request: SynthesisRequest) -> Any | None:  # pyright: ignore[reportExplicitAny]
        """Build VoiceSettings from request fields, or None for defaults."""
        kwargs: dict[str, Any] = {}
        if request.stability is not None:
            kwargs["stability"] = request.stability
        if request.similarity is not None:
            kwargs["similarity_boost"] = request.similarity
        if request.style is not None:
            kwargs["style"] = request.style
        if request.speaker_boost is not None:
            kwargs["use_speaker_boost"] = request.speaker_boost

        if not kwargs:
            return None

        from elevenlabs.types import (
            VoiceSettings,  # pyright: ignore[reportMissingTypeStubs]
        )

        return VoiceSettings(  # pyright: ignore[reportUnknownVariableType, reportCallIssue]
            **kwargs,
        )

    def _single_synthesize(
        self,
        text: str,
        output_path: Path,
        voice_id: str,
        request: SynthesisRequest,
    ) -> None:
        """Synthesize a single chunk to a file."""
        output_path.parent.mkdir(parents=True, exist_ok=True)

        voice_settings = self._build_voice_settings(request)

        kwargs: dict[str, Any] = {
            "voice_id": voice_id,
            "text": text,
            "model_id": self._model,
            "output_format": "mp3_44100_128",
        }
        if voice_settings is not None:
            kwargs["voice_settings"] = voice_settings

        response: Any = self._client.text_to_speech.stream(**kwargs)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

        logger.info(
            "API call: provider=elevenlabs, voice=%s, chars=%d",
            voice_id,
            len(text),
        )
        with output_path.open("wb") as f:
            for chunk in response:  # pyright: ignore[reportUnknownVariableType]
                f.write(chunk)  # pyright: ignore[reportUnknownArgumentType]
