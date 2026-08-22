"""Turn detection: accumulated audible-run duration, not a consecutive-chunk streak.

Implements the model ``docs/conversation-mode-prd.tex`` (S:design-turn)
settled on after two failed Spike 5 attempts: a run of audible chunks
accumulates duration across brief within-word amplitude dips, and only a
genuine silence gap (default 200ms) closes it. A closed run signals
``TURN_ENDED`` only if it accumulated at least ``min_speech_s`` -- a cough or
a click that never reaches that floor is silently absorbed (FR-6), and
steady low-level room noise never crosses the calibrated audible threshold
in the first place (FR-7).

Calibration precedes tuning, per the same section: thresholds are relative
to a noise floor sampled from real chunks at call start
(:meth:`TurnDetector.calibrate`), not hardcoded absolute levels.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from typing import Self, final

from punt_vox.voxd.conversation_mode.audio_chunk import AudioChunk
from punt_vox.voxd.conversation_mode.turn_signal import TurnSignal

__all__ = ["TurnDetector"]

# Multiplier applied to the calibrated noise floor to obtain the "this chunk
# is audible speech, not ambient room noise" threshold (FR-7). A floor of
# zero (silent calibration input, e.g. synthetic all-zero test chunks) is
# floored to this absolute value so the detector never divides into an
# always-triggering zero threshold.
_MIN_NOISE_FLOOR = 1e-6


@final
class TurnDetector:
    """Detect end-of-turn from a stream of caller-timed :class:`AudioChunk` values.

    Never reads the wall clock and never imports an audio-capture library --
    every duration is a parameter on :meth:`process`, so a test drives the
    detector with synthetic chunks and deterministic timing (per the
    testability requirement this class exists to satisfy).
    """

    __slots__ = (
        "_accumulated_speech_s",
        "_audible_multiplier",
        "_min_speech_s",
        "_noise_floor",
        "_silence_gap_s",
        "_silence_run_s",
    )
    _silence_gap_s: float
    _min_speech_s: float
    _audible_multiplier: float
    _noise_floor: float
    _accumulated_speech_s: float
    _silence_run_s: float

    def __new__(
        cls,
        *,
        silence_gap_s: float = 0.2,
        min_speech_s: float = 0.3,
        audible_multiplier: float = 2.0,
    ) -> Self:
        self = super().__new__(cls)
        self._silence_gap_s = silence_gap_s
        self._min_speech_s = min_speech_s
        self._audible_multiplier = audible_multiplier
        self._noise_floor = _MIN_NOISE_FLOOR
        self._accumulated_speech_s = 0.0
        self._silence_run_s = 0.0
        return self

    def calibrate(self, chunks: Sequence[AudioChunk]) -> None:
        """Set the noise floor from *chunks*, a few seconds of ambient room audio.

        Uses the median RMS level, not the mean, so one transient (a chair
        creak, a keyboard clack) during calibration does not skew the floor
        the way an outlier-sensitive mean would.
        """
        if not chunks:
            msg = "calibrate requires at least one chunk"
            raise ValueError(msg)
        levels = sorted(self._rms(chunk.pcm) for chunk in chunks)
        median = levels[len(levels) // 2]
        self._noise_floor = max(median, _MIN_NOISE_FLOOR)

    def process(self, chunk: AudioChunk) -> TurnSignal:
        """Feed one chunk to the detector; return its verdict.

        Above the calibrated audible threshold, the chunk's duration joins
        the current run and any silence-gap timer resets (a brief within-word
        dip does not reset the run itself, only the requirement that the
        *next* silence be a genuine gap to close it). At or below threshold,
        the silence-gap timer accumulates instead; once it reaches
        ``silence_gap_s`` the run closes, reporting ``TURN_ENDED`` if it had
        accumulated ``min_speech_s`` or more, or being silently discarded
        (as ordinary quiet, not a turn) otherwise.
        """
        threshold = self._noise_floor * self._audible_multiplier
        if self._rms(chunk.pcm) > threshold:
            self._accumulated_speech_s += chunk.duration_s
            self._silence_run_s = 0.0
            return TurnSignal.SPEECH_CONTINUING

        self._silence_run_s += chunk.duration_s
        # Floating-point accumulation of many small durations drifts below
        # the true sum (e.g. ten 0.02s chunks land at 0.19999999999999998,
        # not 0.2) -- a tolerance keeps a gap that is exactly the configured
        # length from being spuriously rejected as "not quite there yet".
        if self._silence_run_s < self._silence_gap_s - 1e-9:
            return TurnSignal.SILENCE

        accumulated = self._accumulated_speech_s
        self._accumulated_speech_s = 0.0
        self._silence_run_s = 0.0
        if accumulated >= self._min_speech_s:
            return TurnSignal.TURN_ENDED
        return TurnSignal.SILENCE

    @staticmethod
    def _rms(pcm: bytes) -> float:
        """Return the root-mean-square amplitude of *pcm*, normalized to [0, 1].

        Interprets *pcm* as 16-bit signed little-endian mono samples --
        vox's capture format throughout Conversation Mode. Empty input (a
        zero-length chunk) has no signal to measure, so it reports 0.0
        rather than dividing by zero.
        """
        if not pcm:
            return 0.0
        sample_count = len(pcm) // 2
        samples = struct.unpack(f"<{sample_count}h", pcm[: sample_count * 2])
        mean_square = sum(s * s for s in samples) / sample_count
        return float(mean_square**0.5) / 32768.0
