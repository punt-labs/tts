"""The ``Program`` entity -- the mutable aggregate the daemon and clients drive.

``Program`` owns the current :class:`ProgramState` and the injected
:class:`PlaybackPolicy`, exposing one method per Z operation. Each method checks
the operation's precondition (raising :class:`GuardViolationError` -- the Z
guard, a ``ValueError`` subtype the single writer treats as a lost race), computes
the successor state through :meth:`ProgramState.with_updates` (which re-validates
every invariant), and stores it. The transient-error backoff transitions (the Z
``retrying``/``failed`` sub-machine) are delegated to :class:`RetryMachine`, which
computes their successors; ``Program`` only stores what it returns. Mutators
return ``None`` (PY-OP-8). No method takes or checks a session: ``voxd`` state is
machine-universal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn, Self, final

from punt_vox.types_programs.mode import Mode, PlaybackStatus
from punt_vox.types_programs.status import ProgramStatus
from punt_vox.types_programs.status_views import (
    FailedPartView,
    GenerationStatus,
    NowPlaying,
)
from punt_vox.voxd.programs.guard import GuardViolationError
from punt_vox.voxd.programs.part import FrozenParts, Part
from punt_vox.voxd.programs.playback_policy import Advance, PlaybackPolicy
from punt_vox.voxd.programs.retry_machine import RetryMachine
from punt_vox.voxd.programs.state import ProgramState

if TYPE_CHECKING:
    from punt_vox.types_programs.identifiers import ProgramName, Reason
    from punt_vox.types_programs.playback_fault import PlaybackFault

__all__ = ["Program"]

_PLAYING_MODES = frozenset({Mode.PLAYING_FILLING, Mode.PLAYING_ROTATING, Mode.RETRYING})
"""The modes in which the consume-only cursor may advance."""


@final
class Program:
    """A named audio Program: state plus the advance strategy that drives it."""

    __slots__ = ("_policy", "_state")
    _state: ProgramState
    _policy: PlaybackPolicy

    def __new__(cls, state: ProgramState, policy: PlaybackPolicy) -> Self:
        self = super().__new__(cls)
        self._state = state
        self._policy = policy
        return self

    # -- generation path (Z section 7) -------------------------------------

    def turn_on(self) -> None:
        """Enter the pool from ``off`` (Z ``TurnOn``)."""
        if self._state.mode is not Mode.OFF:
            self._reject("turn_on requires mode off")
        act = self._state.activation(self._state.pool)
        self._state = self._state.with_updates(
            failed_parts=FrozenParts.empty(),
            last_played=None,
            attempts=0,
            last_error=None,
            mode=act.mode,
            filling=act.filling,
            playing=act.playing,
        )

    def first_track_ok(self, new: Part) -> None:
        """Record the first Part and start playing it (Z ``FirstTrackOk``)."""
        if self._state.mode is not Mode.GENERATING_FIRST:
            self._reject("first_track_ok requires mode generating_first")
        if new in self._state.failed_parts:
            self._reject("part already recorded as failed")
        pool = frozenset({new})
        act = self._state.activation(pool)
        self._state = self._state.with_updates(
            pool=pool,
            playing=act.playing,
            last_played=None,
            mode=act.mode,
            filling=act.filling,
            attempts=0,
            last_error=None,
        )

    def first_track_bad_prompt(self, bad: Part, reason: Reason) -> None:
        """Fail permanently on the first Part (Z ``FirstTrackBadPrompt``)."""
        if self._state.mode is not Mode.GENERATING_FIRST:
            self._reject("first_track_bad_prompt requires mode generating_first")
        if bad in self._state.failed_parts:
            self._reject("part already recorded as failed")
        self._state = self._state.with_updates(
            pool=frozenset(),
            failed_parts=self._state.failed_parts.with_failure(bad, reason),
            playing=None,
            last_played=None,
            mode=Mode.FAILED,
            filling=False,
            attempts=0,
            last_error=reason,
        )

    def first_track_transient(self, reason: Reason) -> None:
        """Back off after a transient first-Part error (Z ``FirstTrackTransient``)."""
        self._state = RetryMachine(self._state).first_track_transient(reason)

    def fill_ok(self, new: Part) -> None:
        """Deliver one background Part, stopping the fill at full (Z ``FillOk``)."""
        if not (self._state.mode is Mode.PLAYING_FILLING and self._state.filling):
            self._reject("fill_ok requires an active fill in playing_filling")
        if new in self._state.pool:
            self._reject("part already in pool")
        if new in self._state.failed_parts:
            self._reject("part already recorded as failed")
        new_pool = self._state.pool | {new}
        act = self._state.activation(new_pool)
        self._state = self._state.with_updates(
            pool=new_pool,
            mode=act.mode,
            filling=act.filling,
            attempts=0,
            last_error=None,
        )

    def fill_bad_part(self, bad: Part, reason: Reason) -> None:
        """Record a per-Part permanent failure while playing on (Z ``FillBadPart``)."""
        if not (self._state.mode is Mode.PLAYING_FILLING and self._state.filling):
            self._reject("fill_bad_part requires an active fill in playing_filling")
        if bad in self._state.pool:
            self._reject("part already in pool")
        if bad in self._state.failed_parts:
            self._reject("part already recorded as failed")
        self._state = self._state.with_updates(
            failed_parts=self._state.failed_parts.with_failure(bad, reason),
        )

    def fill_transient(self, reason: Reason) -> None:
        """Back off on a transient fill error, playing on (Z ``FillTransient``)."""
        self._state = RetryMachine(self._state).fill_transient(reason)

    # -- resilience path (Z retrying/failed, via RetryMachine) -------------

    def retry_fails(self, reason: Reason) -> None:
        """Count another transient error below the cap (Z ``RetryFails``)."""
        self._state = RetryMachine(self._state).retry_fails(reason)

    def retry_exhausted(self, reason: Reason) -> None:
        """Give up an empty-pool Program at the cap (Z ``RetryExhausted``)."""
        self._state = RetryMachine(self._state).retry_exhausted(reason)

    def retry_capped(self, reason: Reason) -> None:
        """Tolerate a transient at the cap on a non-empty pool (Z ``RetryCapped``)."""
        self._state = RetryMachine(self._state).retry_capped(reason)

    def recover(self) -> None:
        """Clear the backoff and resume generation (Z ``Recover``)."""
        self._state = RetryMachine(self._state).recover()

    def vibe_style_change(self, new_pool: frozenset[Part]) -> None:
        """Retune to a new (vibe, style) key's saved pool (Z ``VibeStyleChange``)."""
        if self._state.mode is Mode.OFF:
            self._reject("vibe_style_change requires an active program")
        act = self._state.activation(new_pool)
        self._state = self._state.with_updates(
            pool=new_pool,
            failed_parts=FrozenParts.empty(),
            last_played=None,
            attempts=0,
            last_error=None,
            mode=act.mode,
            filling=act.filling,
            playing=act.playing,
        )

    def turn_off(self) -> None:
        """Cancel the fill and stop playback (Z ``TurnOff``)."""
        if self._state.mode is Mode.OFF:
            self._reject("turn_off requires an active program")
        self._state = self._state.with_updates(
            failed_parts=FrozenParts.empty(),
            playing=None,
            last_played=None,
            mode=Mode.OFF,
            filling=False,
            attempts=0,
            last_error=None,
        )

    # -- consume path (Z section 8, no generation) -------------------------

    def rotate(self) -> None:
        """Advance to another ready Part (Z ``Rotate`` = skip = next = loop = end).

        One ungated transition: an automatic track-end advance and a
        user-driven skip are indistinguishable, and any client drives either.
        The next-Part choice is delegated to the injected policy; the resulting
        Part is validated against the pool by the successor's S4 invariant.
        """
        if self._state.mode not in _PLAYING_MODES:
            self._reject("rotate requires a playing mode")
        if len(self._state.pool) < 1:
            self._reject("rotate requires a non-empty pool")
        result = self._policy.next_part(self._state.ordered_pool, self._state.playing)
        if not isinstance(result, Advance):
            msg = "playlist policy signalled COMPLETE, which a playlist has no end for"
            raise AssertionError(msg)
        self._state = self._state.with_updates(
            last_played=self._state.playing,
            playing=result.part,
        )

    def play_part(self, target: Part) -> None:
        """Play a specific ready Part by name, without anti-repeat (Z ``PlayPart``).

        The user asked for this Part, so it plays even if it equals the current
        or just-played Part -- the anti-repeat policy is bypassed.
        """
        if self._state.mode not in _PLAYING_MODES:
            self._reject("play_part requires a playing mode")
        if target not in self._state.pool:
            self._reject("play_part target must be a ready Part")
        self._state = self._state.with_updates(
            last_played=self._state.playing,
            playing=target,
        )

    def start_from_disk(self, target: Part) -> None:
        """Cold-start playback from a saved pool with no fill (Z ``StartFromDisk``).

        A partial pool enters ``playing_filling`` with the fill inactive -- the
        one place ``filling`` is decoupled from the playing mode.
        """
        if self._state.mode is not Mode.OFF:
            self._reject("start_from_disk requires mode off")
        if target not in self._state.pool:
            self._reject("start_from_disk target must be a ready Part")
        act = self._state.activation(self._state.pool)
        self._state = self._state.with_updates(
            playing=target,
            last_played=None,
            mode=act.mode,
            filling=False,
            attempts=0,
            last_error=None,
        )

    # -- observation -------------------------------------------------------

    @property
    def state(self) -> ProgramState:
        """Return the current immutable state (the source for a status view)."""
        return self._state

    @property
    def mode(self) -> Mode:
        """Return the fine-grained lifecycle mode."""
        return self._state.mode

    @property
    def status(self) -> PlaybackStatus:
        """Return the coarse, client-facing playback status."""
        return self._state.mode.status

    @property
    def playing(self) -> Part | None:
        """Return the Part currently playing, or ``None``."""
        return self._state.playing

    @property
    def wants_generation(self) -> bool:
        """Whether the fill reconciler should keep generation running.

        ``filling`` OR ``retrying`` -- the load-bearing clause: a transient
        backoff has ``filling`` false but must keep the retry engine running, so
        a single transient error never strands the Program.
        """
        return self._state.filling or self._state.mode is Mode.RETRYING

    @property
    def advances_on_end(self) -> bool:
        """Whether the loop should auto-advance on a track end (the mode gate)."""
        return self._state.mode in _PLAYING_MODES

    @property
    def pool(self) -> tuple[Part, ...]:
        """Return the ready Parts sorted by intrinsic index (stable order)."""
        return self._state.ordered_pool

    @property
    def failed_parts(self) -> FrozenParts:
        """Return the permanently-failed Parts and their reasons."""
        return self._state.failed_parts

    def to_status(
        self,
        name: ProgramName | None,
        playback_error: PlaybackFault | None = None,
    ) -> ProgramStatus:
        """Assemble this Program's runtime status for a client.

        Populates all three failure surfaces (program-level error, per-Part
        failures, playback fault) into the neutral ``ProgramStatus`` value any
        client parses. ``name`` is the active manifest handle the pure domain
        does not carry; ``playback_error`` is the daemon's live player fault.
        """
        state = self._state
        error = None if state.last_error is None else str(state.last_error)
        return ProgramStatus(
            format=state.format,
            mode=state.mode,
            generation=GenerationStatus(
                filling=state.filling, attempts=state.attempts, last_error=error
            ),
            name=name,
            now_playing=self._now_playing(),
            failed_parts=tuple(
                FailedPartView(index=part.index, reason=str(reason))
                for part, reason in state.failed_parts.ordered()
            ),
            playback_error=playback_error,
        )

    def _now_playing(self) -> NowPlaying | None:
        """Return the "Part N of M" view (1-based pool position), or None.

        ``N`` is the playing Part's position in the ordered ready pool so a
        gapped pool reports "part 3 of 3", never the intrinsic "4 of 3".
        """
        playing = self.playing
        if playing is None:
            return None
        return NowPlaying(index=self.pool.index(playing) + 1, of=len(self.pool))

    @staticmethod
    def _reject(message: str) -> NoReturn:
        """Raise a :class:`GuardViolationError` -- a violated Z precondition (guard)."""
        GuardViolationError.reject(message)
