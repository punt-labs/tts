"""Synthesis pipeline -- TTS provider orchestration, caching, and direct play."""
# pyright: reportPrivateUsage=false
# Internal module within the voxd package -- cross-module private access is expected.

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import tempfile
import time
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Self

from punt_vox.cache import CacheKey, cache_get, cache_put
from punt_vox.core import TTSClient
from punt_vox.normalize import VIBE_TAG_RE, normalize_for_speech
from punt_vox.providers import get_provider
from punt_vox.types import (
    AudioProviderId,
    AudioRequest,
    DirectPlayProvider,
    TTSProvider,
)
from punt_vox.types_synthesis import SynthesisSpec
from punt_vox.voxd.synthesis_result import SynthesisOutcome

__all__ = [
    "_LOCAL_PROVIDERS",
    "_PROVIDER_API_KEY_VAR",
    "SynthesisPipeline",
    "_build_audio_request",
    "_run_play_directly_sync",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Providers that synthesize audio directly to the default device. Cloud
# providers are skipped entirely so we don't pay for provider construction
# only to discover they don't implement play_directly.
_LOCAL_PROVIDERS: frozenset[str] = frozenset({"espeak", "say"})

# Map of provider name to its expected API key env var. Used by the
# direct-play env-injection helper.
_PROVIDER_API_KEY_VAR: dict[str, str] = {
    "elevenlabs": "ELEVENLABS_API_KEY",
    "openai": "OPENAI_API_KEY",
}


@contextlib.contextmanager
def _api_key_context(api_key: str | None, provider_name: str) -> Generator[None]:
    """Temporarily inject api_key into os.environ for the provider."""
    env_key_name = _PROVIDER_API_KEY_VAR.get(provider_name)
    old_key: str | None = None
    if api_key and env_key_name:
        old_key = os.environ.get(env_key_name)
        os.environ[env_key_name] = api_key
    try:
        yield
    finally:
        if api_key and env_key_name:
            if old_key is not None:
                os.environ[env_key_name] = old_key
            else:
                os.environ.pop(env_key_name, None)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _build_audio_request(
    normalized_text: str,
    voice: str | None,
    language: str | None,
    rate: int | None,
    stability: float | None,
    similarity: float | None,
    style: float | None,
    *,
    speaker_boost: bool | None,
    provider_id: str,
) -> AudioRequest:
    """Build an AudioRequest from parsed message fields."""
    return AudioRequest(
        text=normalized_text,
        voice=voice,
        language=language,
        rate=rate,
        stability=stability,
        similarity=similarity,
        style=style,
        speaker_boost=speaker_boost,
        provider=AudioProviderId(provider_id)
        if provider_id in AudioProviderId.__members__
        else None,
    )


def _run_play_directly_sync(
    provider_name: str,
    api_key: str | None,
    provider_factory: Callable[[], TTSProvider],
    request: AudioRequest,
) -> int | None:
    """Construct provider and call ``play_directly`` on a worker thread.

    Returns ``None`` if the provider does not implement the
    ``DirectPlayProvider`` protocol -- the caller will fall back to the
    synthesize-and-queue path. For local providers (say, espeak) whose
    direct play has been extracted to ``local_play.py``, constructs the
    appropriate direct player sharing the provider's VoiceResolver.
    """
    with _api_key_context(api_key, provider_name):
        provider = provider_factory()

        # Local providers have extracted direct players that share
        # the same VoiceResolver instance as the synthesis provider.
        if provider_name == "say":
            from punt_vox.providers.say import SayProvider

            if isinstance(provider, SayProvider):
                from punt_vox.providers.local_play import SayDirectPlayer

                say_player = SayDirectPlayer(voices=provider._voices)  # pyright: ignore[reportPrivateUsage]
                return say_player.play_directly(request)

        if provider_name == "espeak":
            from punt_vox.providers.espeak import EspeakProvider

            if isinstance(provider, EspeakProvider):
                from punt_vox.providers.local_play import EspeakDirectPlayer

                espeak_player = EspeakDirectPlayer(
                    binary=provider._binary,  # pyright: ignore[reportPrivateUsage]
                    voices=provider._voices,  # pyright: ignore[reportPrivateUsage]
                )
                return espeak_player.play_directly(request)

        # Cloud providers and any other provider that implements
        # DirectPlayProvider directly.
        if isinstance(provider, DirectPlayProvider):
            return provider.play_directly(request)

        return None


# ---------------------------------------------------------------------------
# SynthesisPipeline
# ---------------------------------------------------------------------------


class SynthesisPipeline:
    """Coordinate TTS synthesis, caching, and direct-play dispatch."""

    __slots__ = ("_env_lock", "_playback_mutex")

    _env_lock: asyncio.Lock
    _playback_mutex: asyncio.Lock

    def __new__(cls, *, playback_mutex: asyncio.Lock) -> Self:
        self = super().__new__(cls)
        self._env_lock = asyncio.Lock()
        self._playback_mutex = playback_mutex
        return self

    # -- Static helpers ----------------------------------------------------

    @staticmethod
    def model_supports_expressive_tags(provider_name: str, model: str | None) -> bool:
        """Whether the given provider+model combo interprets bracket-style tags.

        Pure lookup: does NOT construct the provider or touch any SDK client,
        so it can run before voxd enters the env-mutation lock that the real
        synthesize path needs. ElevenLabs is the only provider whose answer
        depends on the model -- all others return False unconditionally.

        The ``ElevenLabsProvider`` import is deferred inside the method so
        voxd does not eagerly load the ElevenLabs SDK at module import time
        on systems whose users only ever run espeak/say. Mirrors the lazy
        pattern in :mod:`punt_vox.providers`.
        """
        if provider_name == "elevenlabs":
            from punt_vox.providers.elevenlabs import ElevenLabsProvider

            return ElevenLabsProvider.model_supports_expressive_tags(model)
        return False

    @staticmethod
    def apply_vibe_for_synthesis(
        raw_text: str,
        vibe_tags: str | None,
        provider_name: str,
        model: str | None,
    ) -> str:
        """Compose the final synthesis text from raw input + vibe + capability.

        Takes the user's RAW ``raw_text`` (NOT yet normalized). The order of
        operations matters because :func:`punt_vox.normalize.normalize_for_speech`
        discards brackets via its non-prosody-punctuation filter. If we let
        normalization run first, ``[serious] hello`` becomes ``serious hello``
        and the literal word survives into TTS input.

        Non-expressive path:
            Strip ALL vibe tags (any position) via ``VIBE_TAG_RE.sub(...)``,
            collapse/trim whitespace, then normalize the cleaned text. If the
            input contains only tags, return the empty string.

        Expressive path:
            Split text into tag / non-tag segments via ``VIBE_TAG_RE``,
            normalize only the non-tag segments, reassemble with tags intact,
            then prepend session ``vibe_tags``.
        """
        expressive = SynthesisPipeline.model_supports_expressive_tags(
            provider_name, model
        )

        if not expressive:
            # Remove tags at ANY position via the regex directly -- bypass
            # strip_vibe_tags's guard (which returns original text when
            # stripping leaves nothing). For tags-only input like "[serious]"
            # the correct result is empty, not the bare word "serious".
            cleaned = VIBE_TAG_RE.sub("", raw_text)
            cleaned = re.sub(r"  +", " ", cleaned).strip()
            return normalize_for_speech(cleaned) if cleaned else ""

        # Expressive: normalize around tags so they survive at any position.
        # VIBE_TAG_RE has one capturing group, so split() interleaves:
        #   even indices -> plain text, odd indices -> tag word (no brackets).
        segments = VIBE_TAG_RE.split(raw_text)
        rebuilt: list[str] = []
        for i, seg in enumerate(segments):
            if i % 2 == 0:
                # Plain text segment -- normalize it.
                normed = normalize_for_speech(seg)
                if normed:
                    rebuilt.append(normed)
            else:
                # Captured tag word -- restore brackets.
                rebuilt.append(f"[{seg}]")

        body = " ".join(rebuilt)

        parts: list[str] = []
        if vibe_tags:
            parts.append(vibe_tags.strip())
        if body:
            parts.append(body)
        return " ".join(parts)

    # -- Instance methods --------------------------------------------------

    async def synthesize_to_file(
        self,
        text: str,
        spec: SynthesisSpec,
    ) -> SynthesisOutcome:
        """Run TTS synthesis and return the output path plus cache status.

        Handles API key injection, provider construction, and caching, and
        raises on failure. ``cached=True`` marks an on-disk cache hit (no
        TTS call); ``cached=False`` marks fresh audio -- the observability
        signal forwarded to callers. A per-call ``api_key`` bypasses the
        cache on lookup and store (billing isolation); the anonymous path
        uses the MD5-keyed cache. See ``punt_vox.cache``.
        """
        voice = spec.voice
        provider_name = spec.provider or ""
        model = spec.model
        api_key = spec.api_key
        resolved_voice = voice or ""

        # apply_vibe_for_synthesis takes RAW text: it normalizes the body
        # after splitting off bracket tags that normalization would eat.
        normalized = self.apply_vibe_for_synthesis(
            text, spec.vibe_tags, provider_name, model
        )

        # Cache lookup on the anonymous path only; per-call api_key scopes
        # bypass it (billing isolation + CodeQL sensitive-hash avoidance).
        key = CacheKey(normalized, resolved_voice, provider_name)
        if api_key is None:
            cached = cache_get(key)
            if cached is not None:
                # "served from cache", not "synthesized" -- no synthesis work ran,
                # so an operator can tell cache reuse from real synthesis.
                logger.info(
                    "served %d chars from cache (%s)",
                    len(text),
                    provider_name or "system",
                )
                return SynthesisOutcome(path=cached, cached=True)
        # Per-call api_key scopes skip the lookup; the MISS log below records
        # the bypass ("not cached (per-call api_key)") so no separate log here.

        # Serialize env mutation + synthesis to avoid concurrent os.environ races.
        async with self._env_lock:
            with _api_key_context(api_key, provider_name):
                provider = get_provider(provider_name, config_dir=None, model=model)
                request = _build_audio_request(
                    normalized,
                    voice,
                    spec.language,
                    spec.rate,
                    spec.stability,
                    spec.similarity,
                    spec.style,
                    speaker_boost=spec.speaker_boost,
                    provider_id=provider_name,
                )
                client = TTSClient(provider)

                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                    output_path = Path(tmp.name)

                await asyncio.to_thread(client.synthesize, request, output_path)
                self._require_nonempty_output(output_path, spec, len(text))

                # Anonymous path only; log the real cache_put path, never a
                # CACHE_DIR snapshot and never file= when the bytes weren't cached
                # (per-call api_key scopes and refused writes both yield None).
                cached_path = cache_put(key, output_path) if api_key is None else None
                logger.info(
                    "synthesized %d chars with %s%s",
                    len(text),
                    provider_name or "system",
                    ", cached" if cached_path else "",
                )
                return SynthesisOutcome(path=output_path, cached=False)

    def _require_nonempty_output(
        self, output_path: Path, spec: SynthesisSpec, chars_in: int
    ) -> None:
        """Delete and reject a missing or empty synthesis output; caching it would
        poison every identical request, so it fails fast. The caller logs the
        exception (traceback + fields) -- no separate log here.
        """
        try:
            synth_size = output_path.stat().st_size
        except OSError:
            synth_size = -1
        if synth_size > 0:
            return
        output_path.unlink(missing_ok=True)
        msg = (
            f"synthesis produced missing or empty output file: {output_path} "
            f"(provider={spec.provider or ''}, voice={spec.voice or ''}, "
            f"size={synth_size}, chars_in={chars_in})"
        )
        raise RuntimeError(msg)

    async def try_direct_play(
        self,
        text: str,
        spec: SynthesisSpec,
        *,
        record_result: Callable[..., None],
    ) -> int | None | Exception:
        """Attempt direct-to-device playback via the provider.

        Returns one of:
          * an ``int`` exit code (0 on success) when ``play_directly`` ran,
          * ``None`` when the provider opts out of direct play, or
          * an ``Exception`` instance when provider construction or playback
            raised. The caller is responsible for translating the exception
            into a websocket error response.

        The ``_env_lock`` is only acquired when an API key needs to be
        injected. Local providers (espeak, say) take a fast path with no
        cross-request blocking. Audio playback never holds the lock.
        """
        voice = spec.voice
        provider_name = spec.provider or ""
        model = spec.model

        # apply_vibe_for_synthesis takes RAW text and runs normalize_for_speech
        # on the body itself (after splitting leading bracket tags off, which
        # would otherwise be eaten by normalization).
        normalized = self.apply_vibe_for_synthesis(
            text, spec.vibe_tags, provider_name, model
        )

        request = _build_audio_request(
            normalized,
            voice,
            spec.language,
            spec.rate,
            spec.stability,
            spec.similarity,
            spec.style,
            speaker_boost=spec.speaker_boost,
            provider_id=provider_name,
        )

        def _factory() -> TTSProvider:
            return get_provider(provider_name, config_dir=None, model=model)

        start = time.monotonic()
        try:
            rc = await self._run_direct_play(spec, _factory, request)
        except Exception as exc:
            # provider_name is a raw client field -- %r so an embedded newline
            # cannot forge a second log line at this sink.
            logger.exception("Direct-play raised for provider=%r", provider_name)
            return exc

        if rc is None:
            return None

        elapsed = time.monotonic() - start
        record_result(
            path=Path(f"<direct:{provider_name}>"),
            rc=rc,
            elapsed=elapsed,
            stderr="" if rc == 0 else f"play_directly rc={rc}",
        )
        if rc == 0:
            logger.info(
                "spoke %d chars with %s (%.1fs)",
                len(text),
                provider_name or "system",
                elapsed,
            )
        else:
            logger.error(
                "Direct-play FAILED: provider=%r voice=%r elapsed=%.3fs rc=%d",
                provider_name,
                voice or "",
                elapsed,
                rc,
            )
        return rc

    async def _run_direct_play(
        self,
        spec: SynthesisSpec,
        factory: Callable[[], TTSProvider],
        request: AudioRequest,
    ) -> int | None:
        """Run ``play_directly`` under the playback mutex.

        The ``_env_lock`` is added only when an API key must be injected into the
        environment; local providers take the fast, single-mutex path. The
        ``_playback_mutex`` serializes audible output across every path -- the
        queue consumer holds it too -- so two hooks firing at once cannot overlap
        even though direct play bypasses the queue.
        """
        provider_name = spec.provider or ""
        api_key = spec.api_key
        if api_key and provider_name in _PROVIDER_API_KEY_VAR:
            async with self._env_lock, self._playback_mutex:
                return await asyncio.to_thread(
                    _run_play_directly_sync, provider_name, api_key, factory, request
                )
        async with self._playback_mutex:
            return await asyncio.to_thread(
                _run_play_directly_sync, provider_name, None, factory, request
            )
