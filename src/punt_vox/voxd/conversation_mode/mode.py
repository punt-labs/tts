"""The call's four modes and which detector governs each.

Mirrors ``docs/conversation-mode-call-state.tex`` section 4 exactly: a
totally-defined mapping from :class:`Mode` to :class:`Detector`, so there is
never a mode for which "which detector is active" is an open question.
"""

from __future__ import annotations

from enum import Enum, auto


class Detector(Enum):
    """Which continuous audio process, if any, is live for a given mode."""

    NONE = auto()
    TURN = auto()
    BARGE_IN = auto()


class Mode(Enum):
    """The call's state, per the Z specification's ``Mode`` free type."""

    IDLE = auto()
    LISTENING = auto()
    WAITING = auto()
    SPEAKING = auto()

    @property
    def active_detector(self) -> Detector:
        """Return the detector active while the call is in this mode.

        ``WAITING`` maps to :attr:`Detector.TURN`, the same as
        ``LISTENING`` -- barge-in requires agent speech to interrupt, and
        there is none until :attr:`Mode.SPEAKING`. This is the resolution
        the Z spec's ``activeDetector`` axiom states formally; this
        property is its executable form.
        """
        return _ACTIVE_DETECTOR[self]


_ACTIVE_DETECTOR: dict[Mode, Detector] = {
    Mode.IDLE: Detector.NONE,
    Mode.LISTENING: Detector.TURN,
    Mode.WAITING: Detector.TURN,
    Mode.SPEAKING: Detector.BARGE_IN,
}

__all__ = ["Detector", "Mode"]
