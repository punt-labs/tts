"""Shared conversion utilities for local TTS providers."""

from __future__ import annotations

import subprocess
from pathlib import Path

__all__ = ["estimate_speech_duration_s", "ffmpeg_to_mp3", "rate_to_wpm"]

_DEFAULT_WPM = 175

# A floor on the character-count side of estimate_speech_duration_s: the
# average English word is ~5 letters plus a trailing space, so 175 WPM
# implies roughly this many characters spoken per second in ordinary prose.
# Used only as a lower bound for the *duration*, not as a competing
# estimate of speaking pace -- see that function's docstring.
_CHARS_PER_SECOND = _DEFAULT_WPM * 6 / 60.0


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

    The word-count estimate alone badly underestimates a text dominated by
    one long unbroken token -- a URL, a file path, an identifier -- because
    ``text.split()`` counts it as a single "word" regardless of its length.
    The return value is the larger of the word-count estimate and a
    character-count floor, so a long single token still gets a duration
    proportional to how long it actually takes to read aloud. The floor is
    scaled by *wpm* -- :data:`_CHARS_PER_SECOND` is derived from the default
    pace only, and a floor that ignored a non-default *wpm* would silently
    return the default-pace duration regardless of what pace was requested.

    Raises :class:`ValueError` for ``wpm <= 0`` -- a caller-supplied pace of
    zero divides by zero, and a negative one is not a speaking pace at all;
    both are validated here rather than producing zero or silently wrong
    output (PY-EH-1).
    """
    if wpm <= 0:
        msg = f"wpm must be positive, got {wpm}"
        raise ValueError(msg)
    stripped = text.strip()
    if not stripped:
        return 0.0
    word_count = len(stripped.split())
    words_per_second = wpm / 60.0
    by_words = word_count / words_per_second
    by_chars = len(stripped) / (_CHARS_PER_SECOND * wpm / _DEFAULT_WPM)
    return max(by_words, by_chars)
