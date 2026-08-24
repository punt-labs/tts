"""The dev/test substitute for real microphone capture and ElevenLabs STT.

``vox call start --script`` reads a JSON Lines file of pre-written
utterances (:class:`ScriptedTurn`), builds synthetic speech/silence
:class:`AudioChunk` values sized to close a turn through the *real*
:class:`~punt_vox.voxd.conversation_mode.turn_detector.TurnDetector`, and
feeds them to :class:`ScriptedSTTProvider`, which replays the same script's
text and confidence rather than doing real recognition. No microphone, no
ElevenLabs credentials, no network -- for demos and CI, not the primary way
to place a call (that is the live path in
:mod:`punt_vox.commands.call`, using
:class:`~punt_vox.voxd.conversation_mode.mic_audio_source.MicAudioSource`
and :class:`~punt_vox.providers.elevenlabs_stt.ElevenLabsSTTProvider`).
"""

from __future__ import annotations

import json
import struct
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd.conversation_mode.audio_chunk import AudioChunk
from punt_vox.voxd.conversation_mode.stt_provider import TranscriptEvent
from punt_vox.voxd.conversation_mode.turn_detector import TurnDetector

if TYPE_CHECKING:
    from punt_vox.types import HealthCheck

__all__ = ["ScriptedSTTProvider", "ScriptedTurn"]

_CHUNK_S = 0.02
_SPEECH_CHUNKS = 20  # 400ms of synthetic "speech" per scripted utterance
_SILENCE_CHUNKS = 10  # 200ms of synthetic silence to close the turn


@final
class ScriptedTurn:
    """One line of a ``--script`` file: a scripted utterance and its confidence."""

    __slots__ = ("_confidence", "_text")
    _text: str
    _confidence: float

    def __new__(cls, text: str, confidence: float) -> Self:
        self = super().__new__(cls)
        self._text = text
        self._confidence = confidence
        return self

    @property
    def text(self) -> str:
        return self._text

    @property
    def confidence(self) -> float:
        return self._confidence

    @staticmethod
    def _pcm(amplitude: int, sample_count: int = 320) -> bytes:
        return struct.pack(f"<{sample_count}h", *([amplitude] * sample_count))

    @classmethod
    def silence_chunks(cls, count: int) -> list[AudioChunk]:
        """Return ``count`` chunks of pure silence, for calibration floors."""
        return [AudioChunk(pcm=cls._pcm(0), duration_s=_CHUNK_S) for _ in range(count)]

    @classmethod
    def calibrated_detector(cls) -> TurnDetector:
        """Return a :class:`TurnDetector` calibrated against a synthetic silent floor.

        The scripted (``--script``) path's counterpart to the live path's
        real-ambient-audio calibration
        (:meth:`~punt_vox.commands.call_live_driver.LiveCallDriver.create`):
        the synthetic floor is silence (amplitude 0), matching
        :meth:`synthetic_chunks`'s own silence chunks, so the real
        detector's thresholds are meaningful against the synthetic audio
        the scripted path feeds it.
        """
        detector = TurnDetector()
        detector.calibrate(cls.silence_chunks(10))
        return detector

    @classmethod
    def read_script(cls, path: Path) -> list[ScriptedTurn]:
        """Parse a JSON Lines file of ``{"text": ..., "confidence": ...}`` entries."""
        turns: list[ScriptedTurn] = []
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            payload = json.loads(stripped)
            turns.append(cls(text=payload["text"], confidence=payload["confidence"]))
        return turns

    def synthetic_chunks(self) -> list[AudioChunk]:
        """Return synthetic speech-then-silence chunks that close one turn.

        Stands in for real microphone capture: enough amplitude to cross
        the calibrated audible threshold, for long enough to satisfy
        ``min_speech_s``, followed by a genuine silence gap so the real
        :class:`~punt_vox.voxd.conversation_mode.turn_detector.TurnDetector`
        closes the run on its own logic rather than a shortcut.
        """
        speech = [
            AudioChunk(pcm=self._pcm(20000), duration_s=_CHUNK_S)
        ] * _SPEECH_CHUNKS
        silence = [AudioChunk(pcm=self._pcm(0), duration_s=_CHUNK_S)] * _SILENCE_CHUNKS
        return [*speech, *silence]


@final
class ScriptedSTTProvider:
    """An :class:`STTProvider` seeded by :class:`ScriptedTurn` entries, one at a time.

    Consumed in the same order the CLI feeds :class:`ScriptedTurn` chunks
    through :class:`~punt_vox.voxd.conversation_mode.call_session.CallSession`,
    so the Nth turn's chunks are always transcribed against the Nth script
    line -- the dev/test substitute for
    :class:`~punt_vox.providers.elevenlabs_stt.ElevenLabsSTTProvider`, which
    the live (default) path uses instead.
    """

    __slots__ = ("_index", "_turns")
    _turns: list[ScriptedTurn]
    _index: int

    def __new__(cls, turns: list[ScriptedTurn]) -> Self:
        self = super().__new__(cls)
        self._turns = turns
        self._index = 0
        return self

    @property
    def name(self) -> str:
        return "scripted"

    async def transcribe(
        self, chunks: AsyncIterator[AudioChunk]
    ) -> AsyncIterator[TranscriptEvent]:
        async for _chunk in chunks:
            pass
        turn = self._turns[self._index]
        self._index += 1
        yield TranscriptEvent(text=turn.text, confidence=turn.confidence, is_final=True)

    def check_health(self) -> list[HealthCheck]:
        return []
