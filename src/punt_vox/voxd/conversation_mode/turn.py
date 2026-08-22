"""One human turn, already transcribed, ready to send to an agent session."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["TranscribedTurn"]


@dataclass(frozen=True, slots=True)
class TranscribedTurn:
    """A committed transcript handed to :class:`~.session_attach.SessionAttach`.

    Deliberately minimal: turn detection and speech recognition (Chapter 2 of
    ``docs/conversation-mode-prd.tex``) own confidence scoring, partial-transcript
    self-correction, and the barge-in-carries-forward text (FR-9) -- by the time a
    ``TranscribedTurn`` exists, those concerns are already resolved, and
    session attachment only needs the committed text to forward.
    """

    text: str
