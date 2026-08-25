"""What :class:`~.turn_detector.TurnDetector` reports for one processed chunk."""

from __future__ import annotations

from enum import Enum, auto

__all__ = ["TurnSignal"]


class TurnSignal(Enum):
    """The detector's verdict on the chunk it was just given."""

    SILENCE = auto()
    """Below the audible threshold; no speech accumulating right now."""

    SPEECH_CONTINUING = auto()
    """Above the audible threshold; the current run keeps accumulating."""

    TURN_ENDED = auto()
    """A silence gap closed a run that had accumulated enough speech."""
