"""espeak-ng TTS provider for Linux."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from punt_vox.normalize import strip_vibe_tags
from punt_vox.output import OutputResolver
from punt_vox.providers.convert import ffmpeg_to_mp3, rate_to_wpm
from punt_vox.providers.voice_resolver import VoiceResolver
from punt_vox.types import (
    AudioProviderId,
    HealthCheck,
    SynthesisRequest,
    SynthesisResult,
    TTSProvider,
    VoiceNotFoundError,
)

logger = logging.getLogger(__name__)

__all__ = ["EspeakProvider"]

# Default voice per language (ISO 639-1 -> espeak-ng voice name).
_DEFAULT_VOICES: dict[str, str] = {
    "de": "de",
    "en": "en",
    "es": "es",
    "fr": "fr",
    "it": "it",
    "ja": "ja",
    "ko": "ko",
    "nl": "nl",
    "pl": "pl",
    "pt": "pt",
    "ru": "ru",
    "sv": "sv",
    "tr": "tr",
    "zh": "zh",
}


@dataclass(frozen=True)
class EspeakVoiceConfig:
    """Maps an espeak-ng voice to its language."""

    name: str
    language: str


def _find_espeak_binary() -> str | None:
    """Find the espeak-ng or espeak binary on PATH."""
    for name in ("espeak-ng", "espeak"):
        path = shutil.which(name)
        if path is not None:
            return path
    return None


def _parse_voice_line(
    line: str, lang_col: int, voice_col: int
) -> tuple[str, str, EspeakVoiceConfig] | None:
    """Parse a single voice line from espeak --voices output.

    Returns ``(voice_key, iso_language, config)`` or None if the line
    cannot be parsed.
    """
    if len(line) <= voice_col:
        return None
    lang_part = line[lang_col:voice_col].split()
    voice_part = line[voice_col:].split()
    if not lang_part or not voice_part:
        return None
    lang = lang_part[0]
    voice_name = voice_part[0]
    iso = lang.split("-")[0]
    if len(iso) != 2:
        return None
    cfg = EspeakVoiceConfig(name=lang, language=iso)
    return voice_name.lower(), iso, cfg


class EspeakProvider(TTSProvider):
    """espeak-ng TTS provider for Linux.

    Implements the TTSProvider protocol using the espeak-ng
    speech synthesizer. Zero configuration required. Produces
    intentionally robotic speech to nudge users toward configuring
    a real provider (ElevenLabs, OpenAI, Polly).

    Works on Linux (and other platforms where espeak-ng is installed).
    """

    _binary: str
    _voices: VoiceResolver[EspeakVoiceConfig]

    def __new__(cls) -> Self:
        binary = _find_espeak_binary()
        if binary is None:
            msg = (
                "espeak-ng or espeak not found on PATH. "
                "Install with: sudo apt-get install espeak-ng"
            )
            raise ValueError(msg)
        self = super().__new__(cls)
        self._binary = binary

        self._voices = VoiceResolver(
            self._fetch_voices,
            default_key="en",
            ttl_seconds=0,
            cooldown_seconds=60,
        )
        return self

    @property
    def name(self) -> str:
        return "espeak"

    @property
    def default_voice(self) -> str:
        """Discover the best available English voice from the system."""
        cache = self._voices._cache  # pyright: ignore[reportPrivateUsage]
        if not cache:
            self._voices._ensure_loaded()  # pyright: ignore[reportPrivateUsage]
            cache = self._voices._cache  # pyright: ignore[reportPrivateUsage]
        for candidate in ("en", "en-us", "en-gb"):
            if candidate in cache:
                return candidate
        # First en-* variant found
        for key in cache:
            if key.startswith("en-"):
                return key
        # Absolute fallback: first voice, or "en" if nothing is installed
        if cache:
            return next(iter(cache))
        return "en"

    @property
    def supports_expressive_tags(self) -> bool:
        return False

    def generate_audio(self, request: SynthesisRequest) -> SynthesisResult:
        output_path = OutputResolver.resolve(request)
        return self.synthesize(request, output_path)

    def synthesize(
        self, request: SynthesisRequest, output_path: Path
    ) -> SynthesisResult:
        """Synthesize text to an MP3 file using espeak-ng.

        Produces WAV via espeak-ng, then converts to MP3 via ffmpeg.
        """
        text = strip_vibe_tags(request.text)

        resolved_voice = request.voice or self.default_voice
        voice_cfg = self._voices.resolve(resolved_voice)
        rate = request.rate if request.rate is not None else 100
        wpm = rate_to_wpm(rate)

        output_path.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = Path(tmp.name)

        try:
            subprocess.run(
                [
                    self._binary,
                    "-v",
                    voice_cfg.name,
                    "-s",
                    str(wpm),
                    "-w",
                    str(wav_path),
                    text,
                ],
                check=True,
                timeout=60,
            )
            logger.info(
                "espeak-ng: voice=%s, wpm=%d, chars=%d",
                voice_cfg.name,
                wpm,
                len(text),
            )

            ffmpeg_to_mp3(wav_path, output_path)
        finally:
            wav_path.unlink(missing_ok=True)

        logger.info("Wrote %s", output_path)

        language = request.language or voice_cfg.language
        return SynthesisResult(
            path=output_path,
            text=text,
            provider=AudioProviderId.espeak,
            voice=voice_cfg.name,
            language=language,
            metadata=request.metadata,
        )

    def resolve_voice(self, name: str, language: str | None = None) -> str:
        """Validate and resolve a voice name to its canonical form."""
        cfg = self._voices.resolve(name)
        if language is not None and cfg.language != language:
            msg = (
                f"Voice '{cfg.name}' does not support language "
                f"'{language}' (supports {cfg.language})"
            )
            raise ValueError(msg)
        return cfg.name

    def check_health(self) -> list[HealthCheck]:
        """Check espeak-ng availability."""
        checks: list[HealthCheck] = []

        binary = _find_espeak_binary()
        if binary:
            binary_name = Path(binary).name
            checks.append(
                HealthCheck(passed=True, message=f"{binary_name}: {binary}"),
            )
            voice = self.default_voice
            try:
                cfg = self._voices.resolve(voice)
                checks.append(
                    HealthCheck(
                        passed=True,
                        message=(f"default voice: {cfg.name} ({cfg.language})"),
                    )
                )
            except VoiceNotFoundError:
                checks.append(
                    HealthCheck(
                        passed=False,
                        message=f"default voice not available: {voice}",
                    )
                )
        else:
            checks.append(
                HealthCheck(
                    passed=False,
                    message=(
                        "espeak-ng/espeak: not found on PATH "
                        "(install with: apt install espeak-ng)"
                    ),
                )
            )

        return checks

    def get_default_voice(self, language: str) -> str:
        """Get the default espeak-ng voice for a language."""
        key = language.lower()
        voice = _DEFAULT_VOICES.get(key)
        if voice is None:
            supported = ", ".join(sorted(_DEFAULT_VOICES))
            msg = f"No default voice for language '{language}'. Supported: {supported}"
            raise ValueError(msg)
        return voice

    def list_voices(self, language: str | None = None) -> list[str]:
        """List available espeak-ng voices."""
        all_names = self._voices.list_all()
        if language is None:
            return all_names
        return sorted(
            name
            for name in all_names
            if self._voices.resolve(name).language == language
        )

    def infer_language_from_voice(self, voice: str) -> str | None:
        """Infer ISO 639-1 language from an espeak-ng voice name."""
        cfg = self._voices.resolve(voice)
        return cfg.language

    def _fetch_voices(self) -> dict[str, EspeakVoiceConfig]:
        """Parse ``espeak-ng --voices`` output and return the voice dict."""
        fresh: dict[str, EspeakVoiceConfig] = {}
        lines = self._voices_lines()
        if not lines:
            return fresh

        # Parse column positions from header to handle variable-width
        # Age/Gender field (may contain "M" or "40 M" with MBROLA).
        header = lines[0]
        lang_col = header.find("Language")
        voice_col = header.find("VoiceName")
        if lang_col < 0 or voice_col < 0:
            return fresh

        for line in lines[1:]:
            parsed = _parse_voice_line(line, lang_col, voice_col)
            if parsed is not None:
                self._register_voice(fresh, parsed)

        logger.debug("Fetched %d voices from espeak-ng", len(fresh))
        return fresh

    @staticmethod
    def _voices_lines() -> list[str]:
        """Return the ``espeak-ng --voices`` output lines, or empty when absent.

        The one place the subprocess boundary lives: a missing binary, a spawn or
        run failure, or a timeout all degrade to no lines, so the caller only ever
        parses well-formed output.
        """
        binary = _find_espeak_binary()
        if binary is None:
            return []
        try:
            result = subprocess.run(
                [binary, "--voices"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
        except (
            subprocess.CalledProcessError,
            FileNotFoundError,
            subprocess.TimeoutExpired,
        ):
            return []
        return result.stdout.splitlines()

    @staticmethod
    def _register_voice(
        fresh: dict[str, EspeakVoiceConfig],
        parsed: tuple[str, str, EspeakVoiceConfig],
    ) -> None:
        """Register one parsed voice under its key, language name, and ISO prefix.

        A bare ISO entry (``lang == iso``, e.g. ``en``) always wins over a
        qualified variant parsed earlier (e.g. ``en-us``); every other key is
        first-write-wins.
        """
        key, iso, cfg = parsed
        fresh.setdefault(key, cfg)
        fresh.setdefault(cfg.name.lower(), cfg)  # by-language-code convenience
        if cfg.name == iso or iso not in fresh:
            fresh[iso] = cfg
