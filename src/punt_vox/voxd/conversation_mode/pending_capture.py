"""Accumulates one open :class:`~.turn_detector.TurnDetector` run's chunks.

:class:`CallSession` owns exactly one :class:`PendingCapture` for the run
currently in progress -- audio captured since the last closed turn (or since
the call started). A run either closes normally (:meth:`close`, called on
``TURN_ENDED``) or is abandoned mid-run when the call's mode moves away from
listening/waiting before the human finishes talking (:meth:`discard`, called
from a transition observer) -- see :mod:`.call_session`'s own
``_discard_pending_capture`` for why an abandoned run must never survive into
the next one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.audio_chunk import AudioChunk

__all__ = ["PendingCapture"]


@final
class PendingCapture:
    """One open run's accumulated chunks and whether its speech has started.

    ``in_progress`` tracks the *run*, not the previous chunk:
    :class:`~.turn_detector.TurnDetector` tolerates brief within-word
    amplitude dips shorter than its silence-gap threshold, so one real turn
    can report ``SPEECH_CONTINUING -> SILENCE -> SPEECH_CONTINUING`` within
    itself. Comparing only to the immediately-previous chunk would re-fire
    :meth:`note_speech` on every such dip.
    """

    __slots__ = ("_chunks", "_in_progress")
    _chunks: list[AudioChunk]
    _in_progress: bool

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._chunks = []
        self._in_progress = False
        return self

    @property
    def in_progress(self) -> bool:
        """Return whether this run's speech has already been marked started."""
        return self._in_progress

    def note_speech(self) -> bool:
        """Record a ``SPEECH_CONTINUING`` chunk; return whether this is the rising edge.

        ``True`` only the first time this is called for the current run --
        every call after that (including across a mid-turn silence dip)
        returns ``False``, until the run :meth:`close`\\ s or is
        :meth:`discard`\\ ed.
        """
        if self._in_progress:
            return False
        self._in_progress = True
        return True

    def append(self, chunk: AudioChunk) -> None:
        """Accumulate *chunk* into the run currently in progress."""
        self._chunks.append(chunk)

    def close(self) -> list[AudioChunk]:
        """Return this run's accumulated chunks and reset for the next one."""
        chunks, self._chunks = self._chunks, []
        self._in_progress = False
        return chunks

    def discard(self) -> None:
        """Drop an abandoned run's state without returning its chunks.

        Called when the call's mode moves away from listening/waiting
        before this run ever reaches ``TURN_ENDED`` -- see
        :mod:`.call_session`'s ``_discard_pending_capture`` for the concrete
        corruption this prevents.
        """
        self._chunks = []
        self._in_progress = False
