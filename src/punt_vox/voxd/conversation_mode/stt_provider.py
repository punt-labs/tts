"""The seam between captured audio and a transcribed turn.

:class:`STTProvider` mirrors :class:`~punt_vox.types.TTSProvider`'s shape --
a ``name``, a ``check_health`` method matching the same
:class:`~punt_vox.types.HealthCheck` result type -- but its data-producing
method has no ``TTSProvider`` counterpart: speech recognition delivers a
transcript incrementally, as partial results firm up, and each delivered
piece carries its own confidence, which text-to-speech never needs because
its input is already-known text, not something being recognized. FR-21
requires this interface to not assume ElevenLabs is the only implementation;
FR-19 requires callers to distrust a low-confidence result rather than act on
it, which is why confidence rides on every :class:`TranscriptEvent`, not just
the final one.

Production backs this with
:class:`~punt_vox.providers.elevenlabs_stt.ElevenLabsSTTProvider`. Tests
inject a fake.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from punt_vox.types import HealthCheck
    from punt_vox.voxd.conversation_mode.audio_chunk import AudioChunk

__all__ = ["STTProvider", "TranscriptEvent"]


@dataclass(frozen=True, slots=True)
class TranscriptEvent:
    """One incremental speech-recognition result.

    ``confidence`` is in ``[0.0, 1.0]``; FR-19 requires a caller to treat a
    low-confidence event as "ask the human to repeat" rather than fabricate
    an answer from an uncertain guess. ``is_final`` marks the last event for
    one turn's audio -- a caller uses it to know recognition has settled,
    not by inferring completion from the stream simply ending.
    """

    text: str
    confidence: float
    is_final: bool

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            msg = f"confidence must be in [0.0, 1.0], got {self.confidence!r}"
            raise ValueError(msg)


@runtime_checkable
class STTProvider(Protocol):
    """Provider-agnostic interface for speech-to-text engines."""

    @property
    def name(self) -> str:
        """Short identifier for this provider (e.g. 'elevenlabs')."""
        ...

    def transcribe(
        self, chunks: AsyncIterator[AudioChunk]
    ) -> AsyncIterator[TranscriptEvent]:
        """Recognize speech from *chunks*, yielding results as they firm up.

        Returns an async iterator, not an awaited value, so a caller can act
        on partial results (a live captions display, early turn-detector
        corroboration) before recognition of the whole utterance settles.
        """
        ...

    def check_health(self) -> list[HealthCheck]:
        """Run provider-specific health checks.

        Returns:
            List of HealthCheck results.
        """
        ...
