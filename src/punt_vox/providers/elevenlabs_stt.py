"""ElevenLabs-backed :class:`STTProvider` -- batch recognition on a closed turn.

``speech_to_text.convert`` is a batch call, not a streaming one: it accepts a
whole audio file and returns a whole transcript. That matches how this
provider is used -- :class:`~punt_vox.voxd.conversation_mode.turn_detector.TurnDetector`
already accumulates :class:`AudioChunk` values until a turn closes, and only
then does :class:`~punt_vox.voxd.conversation_mode.call_session.CallSession`
hand the accumulated run to :meth:`ElevenLabsSTTProvider.transcribe`. There is
exactly one event to yield per call -- a single final :class:`TranscriptEvent`
-- because the SDK has nothing to say until the whole file has been sent.

Kept out of ``elevenlabs.py`` (PY-OO-2's class-per-module limit): that module
already houses :class:`~punt_vox.providers.elevenlabs.ElevenLabsProvider`
(text-to-speech) plus its private helpers, and speech-to-text is a distinct
concern with its own SDK surface, its own confidence semantics, and its own
health check. Duplicating the small amount of client-construction and
401-to-:class:`ProviderAuthError` logic here (rather than importing it from
:mod:`punt_vox.providers.elevenlabs`) keeps this module's only sibling
dependency an SDK type, not another provider's private helpers.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from io import BytesIO
from typing import TYPE_CHECKING, Self, final

from elevenlabs.client import ElevenLabs
from elevenlabs.core import ApiError
from elevenlabs.types import (
    SpeechToTextChunkResponseModel,
    SpeechToTextWordResponseModel,
)

from punt_vox.types import HealthCheck
from punt_vox.types_provider_errors import ProviderAuthError
from punt_vox.voxd.conversation_mode.audio_chunk import AudioChunk
from punt_vox.voxd.conversation_mode.stt_provider import TranscriptEvent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

__all__ = ["ElevenLabsSTTProvider"]

# Model used for recognition. Distinct model namespace from
# ``providers/elevenlabs.py``'s TTS ``_DEFAULT_MODEL`` -- this SDK only
# accepts "scribe_v1" or "scribe_v2"; scribe_v1 is the stable, generally
# available model. A caller wanting scribe_v2 passes ``model=``.
_DEFAULT_MODEL = "scribe_v1"

# ElevenLabs' fastest input path: raw 16-bit signed little-endian PCM at
# 16kHz mono, with no container framing -- exactly what
# :class:`AudioChunk` already carries, so no WAV header has to be built.
_FILE_FORMAT = "pcm_s16le_16"


@final
class ElevenLabsSTTProvider:
    """ElevenLabs speech-to-text, satisfying the :class:`STTProvider` protocol.

    ``transcribe`` fully drains *chunks* before calling the SDK -- there is
    no way to start a batch request before the last byte of the turn's audio
    is known -- then yields exactly one ``is_final=True`` event. FR-19
    requires a low-confidence result to become "ask the human to repeat",
    never an acted-on guess; this provider's job is only to report an
    honest confidence, never to raise it by inflating one.
    """

    __slots__ = ("_client", "_model")
    _model: str
    _client: ElevenLabs

    def __new__(
        cls,
        *,
        model: str | None = None,
        client: ElevenLabs | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._model = model or _DEFAULT_MODEL
        self._client = client if client is not None else ElevenLabs()
        return self

    @property
    def name(self) -> str:
        return "elevenlabs"

    async def transcribe(
        self, chunks: AsyncIterator[AudioChunk]
    ) -> AsyncIterator[TranscriptEvent]:
        """Recognize the whole turn in *chunks* as one batch call.

        Empty audio (a turn the detector closed with zero accumulated
        chunks) and a transcript the SDK returned with no recognized words
        both report zero confidence -- FR-19 requires the caller to treat
        either as "ask the human to repeat", never to fabricate a guess.
        """
        pcm = bytearray()
        async for chunk in chunks:
            pcm += chunk.pcm
        yield await self._convert(bytes(pcm))

    async def _convert(self, pcm: bytes) -> TranscriptEvent:
        if not pcm:
            return TranscriptEvent(text="", confidence=0.0, is_final=True)

        try:
            # asyncio.to_thread, not a bare call: the SDK's convert() is a
            # synchronous, blocking HTTP call, and this method runs on the
            # call's single event loop -- a direct call here would stall
            # audio capture and control-request handling for the full
            # network round trip.
            response = await asyncio.to_thread(
                self._client.speech_to_text.convert,
                model_id=self._model,
                file=("turn.raw", BytesIO(pcm), "application/octet-stream"),
                file_format=_FILE_FORMAT,
            )
        except ApiError as exc:
            if exc.status_code == 401:
                raise ProviderAuthError("elevenlabs", 401) from exc
            raise

        # The multichannel and webhook response variants of the SDK's return
        # union don't apply here -- this call passes neither
        # ``use_multi_channel`` nor ``webhook``, so only the single-transcript
        # chunk model is ever returned in practice. Narrowing with isinstance
        # (rather than trusting the SDK's own contract) means an unexpected
        # shape reports zero confidence, per FR-19, instead of raising
        # AttributeError deep in a background call.
        if not isinstance(response, SpeechToTextChunkResponseModel):
            logger.warning(
                "elevenlabs speech_to_text.convert returned an unexpected "
                "response type: %s",
                type(response).__name__,
            )
            return TranscriptEvent(text="", confidence=0.0, is_final=True)

        text = response.text
        confidence = self._confidence_from_words(response.words)
        logger.info(
            "API call: provider=elevenlabs (stt), model=%s, chars=%d, confidence=%.3f",
            self._model,
            len(text),
            confidence,
        )
        return TranscriptEvent(text=text, confidence=confidence, is_final=True)

    def check_health(self) -> list[HealthCheck]:
        """Check the ElevenLabs API key is set.

        Mirrors :meth:`~punt_vox.providers.elevenlabs.ElevenLabsProvider.check_health`'s
        key-presence check; does not repeat its subscription-quota probe,
        which is a text-to-speech-character concern that does not apply to
        speech-to-text's per-minute-of-audio billing.
        """
        if not os.environ.get("ELEVENLABS_API_KEY"):
            return [
                HealthCheck(
                    passed=False,
                    message=("ElevenLabs API key: not set (export ELEVENLABS_API_KEY)"),
                )
            ]
        return [HealthCheck(passed=True, message="ElevenLabs API key: set")]

    @staticmethod
    def _confidence_from_words(
        words: list[SpeechToTextWordResponseModel] | None,
    ) -> float:
        """Derive an overall ``[0.0, 1.0]`` confidence from per-word log-probabilities.

        The SDK reports no single transcript-level confidence -- only a
        per-``SpeechToTextWordResponseModel`` ``logprob`` (natural log of that
        entry's probability, always ``<= 0``). The geometric mean of the
        per-word probabilities -- ``exp(mean(logprob))`` -- is the standard way
        to collapse a sequence of log-probabilities into one score without the
        arithmetic mean of the logs itself (which is on the wrong,
        unbounded-below scale for FR-19's ``[0.0, 1.0]`` gate).

        ``words`` interleaves ``type="word"`` entries with ``type="spacing"``
        (and occasionally ``"audio_event"``) entries, and the SDK reports those
        non-word entries with a near-deterministic, near-zero ``logprob`` --
        averaging them in pulls the geometric mean toward 1.0 regardless of how
        confident the actual words were, which is exactly the inflation FR-19
        forbids (this class's own docstring: "never to raise it by inflating
        one"). Only ``type="word"`` entries are averaged.

        No words -- an empty/fully-unintelligible utterance, or a response
        where every entry was non-word -- reports zero: FR-19's floor treats
        that identically to a low-confidence guess, which is correct, there is
        nothing to act on.
        """
        word_entries = [w for w in words or () if w.type == "word"]
        if not word_entries:
            return 0.0
        mean_logprob = sum(w.logprob for w in word_entries) / len(word_entries)
        return min(1.0, max(0.0, math.exp(mean_logprob)))
