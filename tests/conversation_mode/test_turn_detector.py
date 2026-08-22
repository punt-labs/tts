"""Unit tests for :class:`TurnDetector`'s accumulated-run model.

All chunk timing is synthetic and caller-supplied -- no wall clock, no
audio-capture library -- per this slice's binding testability requirement.
"""

from __future__ import annotations

import struct

import pytest

from punt_vox.voxd.conversation_mode.audio_chunk import AudioChunk
from punt_vox.voxd.conversation_mode.turn_detector import TurnDetector
from punt_vox.voxd.conversation_mode.turn_signal import TurnSignal

_CHUNK_S = 0.02  # 20ms chunks, a realistic capture frame size


def _pcm(amplitude: int, sample_count: int = 320) -> bytes:
    """Return *sample_count* 16-bit PCM samples, all at *amplitude*."""
    return struct.pack(f"<{sample_count}h", *([amplitude] * sample_count))


def _silence(sample_count: int = 320) -> AudioChunk:
    return AudioChunk(pcm=_pcm(0, sample_count), duration_s=_CHUNK_S)


def _speech(sample_count: int = 320) -> AudioChunk:
    # Well above any calibrated noise floor built from near-silent chunks.
    return AudioChunk(pcm=_pcm(20000, sample_count), duration_s=_CHUNK_S)


def _room_noise(sample_count: int = 320) -> AudioChunk:
    # Low-level, steady -- what calibrate() should learn as the floor.
    return AudioChunk(pcm=_pcm(500, sample_count), duration_s=_CHUNK_S)


def _detector() -> TurnDetector:
    detector = TurnDetector(silence_gap_s=0.2, min_speech_s=0.3)
    detector.calibrate([_room_noise() for _ in range(10)])
    return detector


def test_steady_room_noise_never_signals_speech() -> None:
    """FR-7: calibrated room noise stays below the audible threshold."""
    detector = _detector()
    for _ in range(50):
        assert detector.process(_room_noise()) == TurnSignal.SILENCE


def test_continuous_speech_then_gap_ends_turn() -> None:
    """FR-5: enough accumulated speech, closed by a real silence gap, ends a turn."""
    detector = _detector()
    # 400ms of speech -- comfortably above min_speech_s=0.3s.
    for _ in range(20):
        assert detector.process(_speech()) == TurnSignal.SPEECH_CONTINUING
    # 200ms of silence closes the gap (silence_gap_s=0.2, 10 * 20ms chunks).
    signals = [detector.process(_silence()) for _ in range(10)]
    assert signals[:-1] == [TurnSignal.SILENCE] * 9
    assert signals[-1] == TurnSignal.TURN_ENDED


def test_brief_within_word_dip_does_not_reset_the_run() -> None:
    """FR-6: a dip shorter than the silence gap does not lose accumulated speech."""
    detector = _detector()
    for _ in range(10):  # 200ms of speech
        detector.process(_speech())
    for _ in range(5):  # 100ms dip -- shorter than the 200ms silence gap
        assert detector.process(_silence()) == TurnSignal.SILENCE
    for _ in range(10):  # another 200ms of speech; run should still be accumulating
        assert detector.process(_speech()) == TurnSignal.SPEECH_CONTINUING
    # Total accumulated speech: 200ms + 200ms = 400ms, above min_speech_s.
    signals = [detector.process(_silence()) for _ in range(10)]
    assert signals[-1] == TurnSignal.TURN_ENDED


def test_a_cough_shorter_than_min_speech_does_not_end_a_turn() -> None:
    """FR-6: a transient that never reaches min_speech_s is silently absorbed."""
    detector = _detector()
    for _ in range(3):  # 60ms of speech-level sound -- a cough, not an utterance
        detector.process(_speech())
    signals = [detector.process(_silence()) for _ in range(10)]
    assert TurnSignal.TURN_ENDED not in signals


def test_calibrate_requires_at_least_one_chunk() -> None:
    detector = TurnDetector()
    with pytest.raises(ValueError, match="at least one chunk"):
        detector.calibrate([])


def test_empty_pcm_chunk_is_silence_not_a_crash() -> None:
    detector = _detector()
    assert detector.process(AudioChunk(pcm=b"", duration_s=_CHUNK_S)) == (
        TurnSignal.SILENCE
    )
