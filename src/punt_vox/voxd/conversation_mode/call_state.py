"""The call state machine: idle/listening/waiting/speaking, one call at a time.

Direct executable form of ``docs/conversation-mode-call-state.tex``: one
:class:`CallState` instance per call (created by :meth:`CallState.started`,
returned to :attr:`~.mode.Mode.IDLE` by :meth:`end_call`), each Z operation
schema realized as one method with the same precondition/postcondition shape.
Every method that lands on :attr:`~.mode.Mode.LISTENING` (:meth:`barge_in`,
:meth:`reply_ends`) discharges ``has_pending_addendum`` explicitly, per the
Z spec's invariant that an addendum can never survive a transition back to
listening unaccounted for.

This class is deliberately *not* thread- or task-safe on its own -- per the
Z spec's "single serialized dispatch point" section, it is safe to call only
from the one task draining :class:`~.call_actor.CallActor`'s command queue.
"""

from __future__ import annotations

from typing import Self, final

from punt_vox.voxd.conversation_mode.mode import Detector, Mode

__all__ = ["CallState", "IllegalTransitionError"]


class IllegalTransitionError(ValueError):
    """Raised when an operation's precondition on :attr:`CallState.mode` fails.

    Each Z operation schema states a precondition on ``mode`` (e.g.
    ``TurnDetected`` requires ``mode = listening``); a caller that violates
    it -- the orchestrator enqueuing the wrong command for the current mode
    -- has a bug, not a recoverable condition, so this raises rather than
    silently no-opping (PY-EH-1: trust invariants, but only once they are
    actually established by validated preconditions like this one).
    """


@final
class CallState:
    """One call's mode and pending-addendum flag, mutated by Z-shaped operations."""

    __slots__ = ("_has_pending_addendum", "_mode")
    _mode: Mode
    _has_pending_addendum: bool

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._mode = Mode.IDLE
        self._has_pending_addendum = False
        return self

    @property
    def mode(self) -> Mode:
        """Return the call's current mode."""
        return self._mode

    @property
    def has_pending_addendum(self) -> bool:
        """Return whether speech captured during ``waiting`` awaits hand-off."""
        return self._has_pending_addendum

    @property
    def current_detector(self) -> Detector:
        """``CurrentDetector``: the detector live in the current mode."""
        return self._mode.active_detector

    def start_call(self) -> None:
        """``StartCall``: idle -> listening, no setup mode in between."""
        self._require(Mode.IDLE, "start_call")
        self._mode = Mode.LISTENING
        self._has_pending_addendum = False

    def end_call(self) -> None:
        """``EndCall``: any active mode -> idle, an explicit hangup."""
        if self._mode is Mode.IDLE:
            msg = "end_call requires an active call, got idle"
            raise IllegalTransitionError(msg)
        self._mode = Mode.IDLE
        self._has_pending_addendum = False

    def timeout_call(self) -> None:
        """``TimeoutCall``: listening -> idle, the bounded-inactivity timeout."""
        self._require(Mode.LISTENING, "timeout_call")
        self._mode = Mode.IDLE
        self._has_pending_addendum = False

    def turn_detected(self) -> None:
        """``TurnDetected``: listening -> waiting, a turn is now in flight."""
        self._require(Mode.LISTENING, "turn_detected")
        self._mode = Mode.WAITING
        self._has_pending_addendum = False

    def capture_during_wait(self) -> None:
        """``CaptureDuringWait``: waiting -> waiting, records a pending addendum."""
        self._require(Mode.WAITING, "capture_during_wait")
        self._has_pending_addendum = True

    def reply_begins(self) -> None:
        """``ReplyBegins``: waiting -> speaking; a pending addendum carries through."""
        self._require(Mode.WAITING, "reply_begins")
        self._mode = Mode.SPEAKING

    def barge_in(self) -> None:
        """``BargeIn``: speaking -> listening; discharges any pending addendum."""
        self._require(Mode.SPEAKING, "barge_in")
        self._mode = Mode.LISTENING
        self._has_pending_addendum = False

    def reply_ends(self) -> None:
        """``ReplyEnds``: speaking -> listening; discharges any pending addendum."""
        self._require(Mode.SPEAKING, "reply_ends")
        self._mode = Mode.LISTENING
        self._has_pending_addendum = False

    def _require(self, expected: Mode, operation: str) -> None:
        if self._mode is not expected:
            msg = f"{operation} requires mode={expected.name}, got {self._mode.name}"
            raise IllegalTransitionError(msg)
