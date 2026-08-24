"""A scripted, subprocess-free ``STTProvider`` fake for Conversation Mode tests.

Same shape as ``tests/conversation_mode/_session_attach_fakes.py``'s
``FakeSessionAttach``: records every call, replays a script rather than doing
real recognition. Unlike a single-string fake, each :class:`TranscriptEvent`
in the script carries its own confidence, so a test can exercise FR-19 (a
low-confidence result must not be acted on) without a real provider.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Self, final

from punt_vox.types import HealthCheck
from punt_vox.voxd.conversation_mode.audio_chunk import AudioChunk
from punt_vox.voxd.conversation_mode.stt_provider import TranscriptEvent

__all__ = ["FakeSTTProvider"]


@final
@dataclass(frozen=True, slots=True)
class _Call:
    """One recorded call: how many chunks the fake was asked to transcribe."""

    chunk_count: int


@final
class FakeSTTProvider:
    """Replays a fixed script of :class:`TranscriptEvent` for every call."""

    __slots__ = ("_calls", "_name", "_script", "_transcribe_error")
    _name: str
    _script: tuple[TranscriptEvent, ...]
    _calls: list[_Call]
    # When set, ``transcribe`` raises this as a transient provider fault --
    # a rate limit, a 5xx, a network blip -- after consuming the chunks it
    # was handed, mirroring how a real provider's own request/response cycle
    # fails only once it has something to fail on.
    _transcribe_error: str | None

    def __new__(
        cls,
        script: Sequence[TranscriptEvent],
        *,
        name: str = "fake",
        transcribe_error: str | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._name = name
        self._script = tuple(script)
        self._calls = []
        self._transcribe_error = transcribe_error
        return self

    @property
    def name(self) -> str:
        return self._name

    async def transcribe(
        self, chunks: AsyncIterator[AudioChunk]
    ) -> AsyncIterator[TranscriptEvent]:
        chunk_count = 0
        async for _ in chunks:
            chunk_count += 1
        self._calls.append(_Call(chunk_count=chunk_count))
        if self._transcribe_error is not None:
            raise RuntimeError(self._transcribe_error)
        for event in self._script:
            yield event

    def check_health(self) -> list[HealthCheck]:
        return [HealthCheck(passed=True, message="fake provider always healthy")]

    def calls(self) -> list[int]:
        """Return the chunk count consumed on each recorded call, in order."""
        return [call.chunk_count for call in self._calls]
