"""Synthesize scripted utterances to raw PCM for the audio-injection test."""

from __future__ import annotations

import shutil
import subprocess
from typing import Self, final


@final
class EspeakSynth:
    """espeak-ng text-to-speech, resampled to S16LE mono PCM at the session rate.

    The Conv AI leg speaks pcm_16000; espeak-ng emits 22050Hz WAV, so the
    output is piped through ffmpeg for the resample. Both binaries are
    checked up front so a missing dependency fails before any billed call.
    """

    _rate: int

    def __new__(cls, rate: int = 16_000) -> Self:
        for binary in ("espeak-ng", "ffmpeg"):
            if shutil.which(binary) is None:
                msg = f"{binary} not found on PATH"
                raise RuntimeError(msg)
        self = super().__new__(cls)
        self._rate = rate
        return self

    def pcm(self, text: str) -> bytes:
        """Return ``text`` spoken as raw S16LE mono PCM at the session rate."""
        # 150wpm (default 175) -- slightly slower speech transcribes better.
        wav = self._run(
            ["espeak-ng", "--stdout", "-v", "en-us", "-s", "150", text], b""
        )
        return self._run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "-ac",
                "1",
                "-ar",
                str(self._rate),
                "pipe:1",
            ],
            wav,
        )

    @staticmethod
    def _run(argv: list[str], stdin: bytes) -> bytes:
        done = subprocess.run(argv, input=stdin, capture_output=True, check=False)
        if done.returncode != 0 or not done.stdout:
            detail = done.stderr.decode(errors="replace")[:300]
            msg = f"{argv[0]} failed: {detail}"
            raise RuntimeError(msg)
        return done.stdout
