"""Audio request and result value types for punt-vox."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from punt_vox.types import AudioProviderId

__all__ = [
    "FETCH_CHUNK_BYTES",
    "FETCH_FRAME_LIMIT_BYTES",
    "AudioRequest",
    "AudioResult",
]

# Per-frame byte bound for the chunked ``fetch`` transport. A store file of any
# total size streams as a sequence of ``chunk`` frames, each at most this many
# raw bytes; base64 inflates ~33%, so 256 KiB stays well under the client's
# default 1 MiB receive limit with room for the JSON envelope. There is
# deliberately no total-file ceiling on the daemon -- the old single-frame limit
# no longer governs retrieval.
FETCH_CHUNK_BYTES = 256 * 1024

# Legacy single-frame ceiling, retained only for the top-level ``record`` locator
# hint in ``__main__.py`` (which the unified-verb CLI mission deletes). The daemon
# ``fetch`` no longer uses it -- retrieval is chunked and unbounded.
FETCH_FRAME_LIMIT_BYTES = 700_000


def _metadata() -> dict[str, str]:
    return {}


@dataclass(frozen=True)
class AudioRequest:
    """Request to synthesize a single audio clip."""

    text: str
    voice: str | None = None
    language: str | None = None
    rate: int | None = None
    stability: float | None = None
    similarity: float | None = None
    style: float | None = None
    speaker_boost: bool | None = None
    provider: AudioProviderId | None = None
    metadata: dict[str, str] = field(default_factory=_metadata)

    @staticmethod
    def validate_language(code: str) -> str:
        """Validate and normalize an ISO 639-1 language code.

        Checks format only (2 lowercase ASCII letters). Does not check
        whether the code is in SUPPORTED_LANGUAGES -- providers decide
        what they support.
        """
        normalized = code.strip().lower()
        if len(normalized) != 2 or not normalized.isascii() or not normalized.isalpha():
            msg = (
                f"Invalid language code '{code}'. "
                "Expected ISO 639-1 format (2 letters, e.g. 'de', 'ko')."
            )
            raise ValueError(msg)
        return normalized

    @staticmethod
    def generate_filename(text: str, prefix: str = "") -> str:
        """Generate a deterministic MP3 filename from text content.

        Uses an MD5 hash of the text to produce a stable, filesystem-safe
        filename. An optional prefix is prepended for disambiguation.
        """
        digest = hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()[:12]
        if prefix:
            return f"{prefix}{digest}.mp3"
        return f"{digest}.mp3"


@dataclass(frozen=True)
class AudioResult:
    """Result of an audio synthesis request."""

    path: Path
    text: str
    provider: AudioProviderId
    voice: str | None = None
    language: str | None = None
    metadata: dict[str, str] = field(default_factory=_metadata)

    def to_dict(self) -> dict[str, str]:
        """Serialize to a dict suitable for MCP tool responses."""
        d: dict[str, str] = {
            "path": str(self.path),
            "text": self.text,
            "provider": self.provider.value,
        }
        if self.voice is not None:
            d["voice"] = self.voice
        if self.language is not None:
            d["language"] = self.language
        if self.metadata:
            d.update(self.metadata)
        return d
