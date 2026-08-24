"""One slice of captured microphone audio, with caller-supplied timing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

__all__ = ["SAMPLE_RATE_HZ", "AudioChunk"]

# The sample rate every producer and consumer of ``AudioChunk.pcm`` agrees on:
# real microphone capture (:mod:`.mic_audio_source`) requests this rate from
# the audio device, and the ElevenLabs STT provider
# (:mod:`punt_vox.providers.elevenlabs_stt`) declares it on the wire via
# ``file_format="pcm_s16le_16"`` (ElevenLabs' name for exactly this format).
# 16kHz mono is standard for speech recognition -- above it buys no
# transcription-accuracy benefit for voice, only bandwidth.
# :class:`~punt_vox.voxd.conversation_mode.turn_detector.TurnDetector` does
# not need this constant itself; its RMS math is scale-invariant to sample
# rate and only cares about each chunk's caller-supplied ``duration_s``.
SAMPLE_RATE_HZ = 16_000


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """16-bit signed mono PCM plus how long it spans, as the caller measured it.

    ``duration_s`` is a parameter, never a wall-clock read -- the turn and
    barge-in detectors (``docs/conversation-mode-prd.tex`` Chapter 3's
    testability requirements) must accept synthetic timing in tests, so
    nothing downstream of capture may call ``time.monotonic()`` itself.
    ``pcm`` is sampled at :data:`SAMPLE_RATE_HZ`.
    """

    pcm: bytes
    duration_s: float

    @classmethod
    async def as_async_iter(
        cls, chunks: Sequence[AudioChunk]
    ) -> AsyncIterator[AudioChunk]:
        """Return an async iterator over *chunks*.

        :class:`~.stt_provider.STTProvider.transcribe` is typed to accept
        ``AsyncIterator[AudioChunk]`` -- a run the turn detector already
        accumulated as a plain list needs this small adapter to satisfy
        that boundary; a classmethod here rather than a free function next
        to this class in another module, since the whole operation is
        about this class's own values.
        """
        for chunk in chunks:
            yield chunk
