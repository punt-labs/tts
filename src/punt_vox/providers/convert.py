"""Shared conversion utilities for local TTS providers."""

from __future__ import annotations

import subprocess
from pathlib import Path

__all__ = ["estimate_speech_duration_s", "ffmpeg_to_mp3", "rate_to_wpm"]

_DEFAULT_WPM = 175


def ffmpeg_to_mp3(input_path: Path, output_path: Path) -> None:
    """Convert an audio file to MP3 via ffmpeg."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-codec:a",
            "libmp3lame",
            "-qscale:a",
            "2",
            str(output_path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )


def rate_to_wpm(rate: int) -> int:
    """Convert percentage rate to words-per-minute.

    rate=100 -> 175 WPM (normal), rate=90 -> 157 WPM.
    """
    return max(1, int(_DEFAULT_WPM * rate / 100))


def estimate_speech_duration_s(text: str, *, wpm: int = _DEFAULT_WPM) -> float:
    """Estimate how long speaking *text* aloud will take, in seconds.

    An estimate, not a measurement -- there is no real TTS timing signal
    available at the call site this exists for (see
    ``punt_vox.commands.call``'s mic-echo gate), so this reuses the same
    normal-speaking-pace baseline (:data:`_DEFAULT_WPM`) this module already
    uses for say/espeak rate conversion. Word count is a naive whitespace
    split, not a linguistic tokenizer -- close enough for a duration this is
    only ever used to bound a wait, not to schedule anything precisely.
    """
    word_count = len(text.split())
    if word_count == 0:
        return 0.0
    words_per_second = wpm / 60.0
    return word_count / words_per_second
