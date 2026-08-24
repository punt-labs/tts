"""Owns one :class:`~.call_state.CallState` and applies commands to it.

Today, :class:`~.call_session.CallSession` is the sole driver of state
transitions -- it processes captured chunks through one sequential
``async for`` loop with no concurrent detector task racing it (there is no
barge-in detector task yet; see that class's own ``barge_in`` docstring), so
:meth:`CallActor.apply` is always called from that single already-serialized
path. ``docs/conversation-mode-call-state.tex`` section 8 anticipates a
future where turn detection, barge-in detection, and sentence-streamed
synthesis are three independent, genuinely concurrent producers that could
all want to change :class:`CallState` at once -- at that point (a future
barge-in detector is the first candidate) this class gets a real
queue-backed serialization point, wired to every producer that then exists.
Building that queue now, with nothing enqueuing into it, would be exactly
the "create now, wire later" dead code this project's own refactoring
protocol forbids (PY-RF-2) -- it is added when the second concurrent
producer actually exists to prove the ordering it protects.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd.conversation_mode.call_state import CallState
from punt_vox.voxd.conversation_mode.mode import Detector, Mode

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.call_command import CallCommand

logger = logging.getLogger(__name__)

__all__ = ["CallActor"]

# A transition-observer callback: (previous mode, new mode) -> None. The
# actor calls this synchronously, after CallState has already applied the
# transition, so an orchestrator can react (speak an audible cue per NFR-6,
# start a turn's session-attach forward, arm a timeout) without the actor
# itself knowing anything about audio, sessions, or timers.
type TransitionObserver = Callable[[Mode, Mode], None]


@final
class CallActor:
    """Owns one :class:`CallState`, applying commands from its sole caller."""

    __slots__ = ("_observers", "_state")
    _state: CallState
    _observers: list[TransitionObserver]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._state = CallState()
        self._observers = []
        return self

    @property
    def mode(self) -> Mode:
        """Return the call's current mode."""
        return self._state.mode

    @property
    def has_pending_addendum(self) -> bool:
        """Return whether speech captured during ``waiting`` awaits hand-off."""
        return self._state.has_pending_addendum

    @property
    def current_detector(self) -> Detector:
        """Return the detector active in the call's current mode."""
        return self._state.current_detector

    def on_transition(self, observer: TransitionObserver) -> None:
        """Register *observer* to be called after every applied transition."""
        self._observers.append(observer)

    def apply(self, command: CallCommand) -> None:
        """Apply *command* to the state immediately and notify observers.

        Correct only when the caller is already the single serialized
        dispatch point -- see this class's own module docstring for why
        that is true today (:class:`~.call_session.CallSession` is the sole
        caller, processing chunks through one sequential loop) and what
        would need to change if a second concurrent producer arrived.

        One observer's exception must not stop the others from running, nor
        propagate into the caller's control flow (a caller applying a
        command has already committed the state transition; an observer
        failing to react to it -- speaking a cue, draining a buffer -- is
        that observer's problem, not a reason to unwind the call). Each is
        run in its own ``try``/``except`` and a failure is logged, not
        raised.
        """
        before = self._state.mode
        command.apply(self._state)
        after = self._state.mode
        for observer in self._observers:
            try:
                observer(before, after)
            except Exception:
                logger.exception(
                    "call transition observer %r raised on %s -> %s",
                    observer,
                    before.name,
                    after.name,
                )
