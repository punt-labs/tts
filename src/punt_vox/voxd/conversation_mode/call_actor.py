"""The single serialized dispatch point the Z specification requires.

``docs/conversation-mode-call-state.tex`` section 8: turn detection,
barge-in detection, and sentence-streamed synthesis are three independent,
continuous producers that may all want to change :class:`CallState`
concurrently. :class:`CallActor` is the resolution -- an actor with an
internal, single-consumer :class:`asyncio.Queue` of commands, not a lock.
Every producer holds a reference to the actor and calls :meth:`enqueue`;
none ever calls a :class:`CallState` method directly, and exactly one task
(:meth:`run`) drains the queue, applying one command at a time with nothing
awaited mid-operation -- the queue analogue of
``voxd/programs/program.py``'s documented single-writer assumption, made
structurally impossible to violate rather than merely stated.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd.conversation_mode.call_state import CallState
from punt_vox.voxd.conversation_mode.mode import Detector, Mode

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.call_command import CallCommand

__all__ = ["CallActor"]

# A transition-observer callback: (previous mode, new mode) -> None. The
# actor calls this synchronously, after CallState has already applied the
# transition, so an orchestrator can react (speak an audible cue per NFR-6,
# start a turn's session-attach forward, arm a timeout) without the actor
# itself knowing anything about audio, sessions, or timers.
type TransitionObserver = Callable[[Mode, Mode], None]


@final
class CallActor:
    """Owns one :class:`CallState`, draining commands one at a time."""

    __slots__ = ("_observers", "_queue", "_state")
    _state: CallState
    _queue: asyncio.Queue[CallCommand | None]
    _observers: list[TransitionObserver]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._state = CallState()
        self._queue = asyncio.Queue()
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

    async def enqueue(self, command: CallCommand) -> None:
        """Queue *command* for the dispatch loop; returns once it is queued.

        Producers (turn detection, barge-in detection, synthesis) call this
        and return immediately -- they never apply a transition themselves.
        """
        await self._queue.put(command)

    async def run(self) -> None:
        """Drain the command queue until :meth:`stop` is called.

        Applies exactly one command per iteration with nothing awaited
        mid-operation, so no other command can interleave inside a single
        transition.
        """
        while True:
            command = await self._queue.get()
            if command is None:
                return
            self._apply(command)

    async def stop(self) -> None:
        """Signal :meth:`run` to return once it has drained pending commands."""
        await self._queue.put(None)

    def _apply(self, command: CallCommand) -> None:
        before = self._state.mode
        command.apply(self._state)
        after = self._state.mode
        for observer in self._observers:
            observer(before, after)
