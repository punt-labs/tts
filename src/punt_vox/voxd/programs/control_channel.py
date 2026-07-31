"""The single-writer control channel -- the one place the active source is mutated.

``ControlChannel`` is a single-consumer command queue. Every mutation of the
active playback source -- user commands, automatic advances, source switches, and
fill outcomes -- is posted as a :class:`ControlSignal` and drained one at a time
by exactly one consumer task that applies it to the current source. This realises
the O2 concurrency contract: the Z state machine is sequential, so two clients
firing ``next`` and ``vibe`` at once (or a fill completing mid-command) can never
interleave or apply against a stale base. A command whose precondition no longer
holds -- a lost race, e.g. a fill outcome that arrives just after a switch to a
replay Selection -- raises a :class:`GuardViolationError`; the consumer logs it at
INFO and moves on rather than dying, so one losing racer cannot take down the
sole writer.

A *non-guard* failure is a different animal: a plain ``ValueError`` from a
corrupt successor state, or any other unexpected exception, means a bug -- not a
race. The consumer never swallows it as a lost race; it surfaces at ERROR
through the ``serve`` guard, always sets ``changed`` first so the
playback loop never blocks forever on a half-applied command, and keeps the sole
writer alive.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd.programs.control_signal import ControlSignal
from punt_vox.voxd.programs.guard import GuardViolationError
from punt_vox.voxd.programs.mode_transition_log import ModeTransitionLog

if TYPE_CHECKING:
    from punt_vox.voxd.programs.change_signal import ChangeSignal
    from punt_vox.voxd.programs.fill_reconciler import FillReconciler
    from punt_vox.voxd.programs.playback_source import PlaybackSource

__all__ = ["ControlChannel"]

logger = logging.getLogger(__name__)


@final
class ControlChannel:
    """Serialize every source mutation through one consumer (single-writer).

    The consumer is also the orchestration point: after each applied command it
    hands the active source to its :class:`FillReconciler`, which starts or
    cancels the background fill to match ``source.wants_generation`` -- so the fill
    lifecycle rides the single writer and never races the source's state.
    """

    __slots__ = (
        "_changed",
        "_changes",
        "_interrupt",
        "_mode_log",
        "_queue",
        "_reconciler",
        "_source",
    )
    _source: PlaybackSource
    _queue: asyncio.Queue[ControlSignal]
    _changed: asyncio.Event
    _interrupt: asyncio.Event
    _reconciler: FillReconciler | None
    _changes: ChangeSignal | None
    _mode_log: ModeTransitionLog

    def __new__(cls, source: PlaybackSource) -> Self:
        self = super().__new__(cls)
        self._source = source
        self._queue = asyncio.Queue()
        self._changed = asyncio.Event()
        self._interrupt = asyncio.Event()
        self._reconciler = None
        self._changes = None
        self._mode_log = ModeTransitionLog(source)
        return self

    def attach_reconciler(self, reconciler: FillReconciler) -> None:
        """Wire the fill reconciler the consumer runs after each applied command."""
        self._reconciler = reconciler

    def attach_change_signal(self, changes: ChangeSignal) -> None:
        """Wire the change signal the consumer fires after each applied command."""
        self._changes = changes

    @property
    def source(self) -> PlaybackSource:
        """Return the active source this channel is the sole writer to."""
        return self._source

    def retarget(self, source: PlaybackSource) -> None:
        """Swap which source the sole writer animates (a switch to program/replay).

        Called only from inside an applied command (a switch on the consumer
        thread), so the swap is serialised with every other mutation -- the loop
        and the fill re-read :attr:`source` after the writer wakes them and see
        the new source, never one half-swapped.
        """
        self._source = source

    @property
    def changed(self) -> asyncio.Event:
        """Return the event set after each applied command (the loop races it)."""
        return self._changed

    @property
    def interrupt(self) -> asyncio.Event:
        """Return the event set when an interrupting command is applied."""
        return self._interrupt

    def post(self, signal: ControlSignal) -> None:
        """Enqueue a command for the single consumer to apply."""
        self._queue.put_nowait(signal)

    async def serve(self) -> None:
        """Drain and apply commands forever -- the sole writer to the source."""
        while True:
            try:
                await self.apply_next()
            except Exception:
                logger.exception("control writer: unexpected error applying a command")

    async def apply_next(self) -> None:
        """Apply exactly one queued command, serializing all mutation.

        The one choke point for observing Program mode transitions: every mutation
        -- user command, automatic advance, or fill outcome -- funnels through
        here, so reading the mode before and after and logging only on change
        captures every transition uniformly, at INFO, without scattering loggers.
        """
        signal = await self._queue.get()
        applied = False
        try:
            applied = self._apply_one(signal)
        finally:
            self._queue.task_done()
            try:
                self._reconcile_fill()
            finally:
                # The wake is unconditional: even a raising reconcile must not
                # leave the playback loop blocked on ``changed``.
                self._mark_applied(signal, applied=applied)
                # The re-push rides the same finally so a raising apply still
                # re-projects the settled state; ``emit`` is fail-soft.
                self._emit_change()
        self._mode_log.note(self._source)

    def _emit_change(self) -> None:
        """Fire the change signal so subscribers re-project the settled state."""
        if self._changes is not None:
            self._changes.emit()

    def _apply_one(self, signal: ControlSignal) -> bool:
        """Apply one command; return whether it took effect (False on lost race)."""
        try:
            signal.apply(self._source)
        except GuardViolationError:
            # A losing racer, not a bug -- log the source + signal for the trail.
            logger.info(
                "control signal rejected as a lost race on %s: %r",
                type(self._source).__name__,
                signal,
            )
            return False
        return True

    def _mark_applied(self, signal: ControlSignal, *, applied: bool) -> None:
        """Wake the loop, interrupting only when a command that demands it applied.

        A rejected command (lost race) that declares ``interrupts`` must NOT fire
        the interrupt: it changed no state, so killing the current track would
        restart it from zero while the cursor stayed put -- a silent no-op that
        sounds like a stutter. ``changed`` still fires unconditionally so the loop
        never blocks on a settled command.
        """
        if applied and signal.interrupts:
            self._interrupt.set()
        self._changed.set()

    def _reconcile_fill(self) -> None:
        """Reconcile the background fill to the active source via the reconciler."""
        if self._reconciler is not None:
            self._reconciler.reconcile(self._source)

    async def join(self) -> None:
        """Block until every posted command has been applied."""
        await self._queue.join()
