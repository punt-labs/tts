"""The executable Z ``Program`` state schema -- a self-validating value object.

``ProgramState`` is the Z state schema of ``docs/audio-programs.tex`` made
executable: a frozen, hashable value object whose constructor validates *every*
invariant in the schema predicate (S1--S16) before returning, so an illegal
state cannot be represented. Transitions never mutate a state; they build a
re-validated successor through :meth:`ProgramState.with_updates`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Self, final

from punt_vox.types_programs.format import Format
from punt_vox.types_programs.identifiers import Reason
from punt_vox.types_programs.mode import Mode
from punt_vox.voxd.programs.invariants import StateInvariants
from punt_vox.voxd.programs.part import FrozenParts, Part

__all__ = ["Activation", "ProgramState"]


@final
class _Unset:
    """Sentinel for "carry this field forward" in :meth:`ProgramState.with_updates`.

    Distinct from ``None`` because ``None`` is a legal value for the optional
    fields (it clears them), so it cannot also mean "unchanged".
    """

    __slots__ = ()


_UNSET: Final = _Unset()


@dataclass(frozen=True, slots=True)
class Activation:
    """The ``(mode, filling, playing)`` a pool's size selects when a Program starts.

    The shared classification behind the six disjunctive transitions
    (``TurnOn``, ``FirstTrackOk``, ``FillOk``, ``VibeStyleChange``,
    ``StartFromDisk``, ``Recover``): empty pool -> generating; partial pool ->
    playing and filling; full pool -> playing and rotating.
    """

    mode: Mode
    filling: bool
    playing: Part | None


@final
class ProgramState:
    """One legal Program state -- all 16 Z invariants hold by construction.

    The nine slots are exactly the nine state variables of the Z ``Program``
    schema. The optional-Part sets of the model (``playing``, ``lastPlayed``)
    and the optional reason (``lastError``) become ``T | None``, the faithful
    at-most-one encoding (invariants S2 and S3 hold structurally). Value
    equality over all nine fields (PY-OP-2) lets tests compare states reached
    by different transition paths.
    """

    __slots__ = (
        "_attempts",
        "_failed_parts",
        "_filling",
        "_format",
        "_last_error",
        "_last_played",
        "_mode",
        "_playing",
        "_pool",
    )
    _format: Format
    _pool: frozenset[Part]
    _failed_parts: FrozenParts
    _playing: Part | None
    _last_played: Part | None
    _mode: Mode
    _filling: bool
    _attempts: int
    _last_error: Reason | None

    def __new__(
        cls,
        *,
        fmt: Format,
        pool: frozenset[Part],
        failed_parts: FrozenParts,
        playing: Part | None,
        last_played: Part | None,
        mode: Mode,
        filling: bool,
        attempts: int,
        last_error: Reason | None,
    ) -> Self:
        self = super().__new__(cls)
        self._format = fmt
        self._pool = pool
        self._failed_parts = failed_parts
        self._playing = playing
        self._last_played = last_played
        self._mode = mode
        self._filling = filling
        self._attempts = attempts
        self._last_error = last_error
        StateInvariants(self).verify()
        return self

    # -- construction (Z Init / RestartFromDisk) ---------------------------

    @classmethod
    def initial(cls) -> Self:
        """Return the idle empty-playlist start state (Z ``Init``)."""
        return cls(
            fmt=Format.PLAYLIST,
            pool=frozenset(),
            failed_parts=FrozenParts.empty(),
            playing=None,
            last_played=None,
            mode=Mode.OFF,
            filling=False,
            attempts=0,
            last_error=None,
        )

    @classmethod
    def restored(cls, fmt: Format, disk_pool: frozenset[Part]) -> Self:
        """Return an idle state over a saved pool of ready Parts (Z restart).

        The Z precondition ``#diskPool <= poolSize`` is enforced by S1 in the
        constructor, which raises ``ValueError`` on an over-full disk pool.
        """
        return cls(
            fmt=fmt,
            pool=disk_pool,
            failed_parts=FrozenParts.empty(),
            playing=None,
            last_played=None,
            mode=Mode.OFF,
            filling=False,
            attempts=0,
            last_error=None,
        )

    # -- observation -------------------------------------------------------

    @property
    def format(self) -> Format:
        """Return the Program format (invariant across every transition)."""
        return self._format

    @property
    def pool(self) -> frozenset[Part]:
        """Return the ready Parts as a set (the Z ``pool``)."""
        return self._pool

    @property
    def ordered_pool(self) -> tuple[Part, ...]:
        """Return the ready Parts sorted by intrinsic index (stable)."""
        return tuple(sorted(self._pool, key=lambda part: part.index))

    @property
    def failed_parts(self) -> FrozenParts:
        """Return the permanently-failed Parts and their reasons (Z ``failedParts``)."""
        return self._failed_parts

    @property
    def playing(self) -> Part | None:
        """Return the Part currently playing, or ``None``."""
        return self._playing

    @property
    def last_played(self) -> Part | None:
        """Return the Part played immediately before, or ``None``."""
        return self._last_played

    @property
    def mode(self) -> Mode:
        """Return the current lifecycle mode."""
        return self._mode

    @property
    def filling(self) -> bool:
        """Return whether a background fill is running (Z ``filling``)."""
        return self._filling

    @property
    def attempts(self) -> int:
        """Return the transient retries in flight (Z ``attempts``)."""
        return self._attempts

    @property
    def last_error(self) -> Reason | None:
        """Return the program-level advisory error, or ``None`` when healthy."""
        return self._last_error

    # -- successor construction --------------------------------------------

    def activation(self, pool: frozenset[Part]) -> Activation:
        """Classify ``pool`` by size into its ``(mode, filling, playing)`` activation.

        The shared spine of the six disjunctive transitions. Callers override
        ``filling`` afterwards where the model requires it -- ``StartFromDisk``
        keeps the fill inactive, ``Recover`` forces it on.
        """
        if not pool:
            return Activation(mode=Mode.GENERATING_FIRST, filling=True, playing=None)
        first = min(pool, key=lambda part: part.index)
        if len(pool) < self._format.pool_size:
            return Activation(mode=Mode.PLAYING_FILLING, filling=True, playing=first)
        return Activation(mode=Mode.PLAYING_ROTATING, filling=False, playing=first)

    def with_updates(
        self,
        *,
        pool: frozenset[Part] | _Unset = _UNSET,
        failed_parts: FrozenParts | _Unset = _UNSET,
        playing: Part | _Unset | None = _UNSET,
        last_played: Part | _Unset | None = _UNSET,
        mode: Mode | _Unset = _UNSET,
        filling: bool | _Unset = _UNSET,
        attempts: int | _Unset = _UNSET,
        last_error: Reason | _Unset | None = _UNSET,
    ) -> ProgramState:
        """Return a re-validated successor with the named fields replaced.

        Every parameter carries its field's exact type, so each call site is
        type-checked; ``format`` is never a parameter because it is
        invariant across all transitions. Unnamed fields carry forward.
        """
        return ProgramState(
            fmt=self._format,
            pool=self._pool if isinstance(pool, _Unset) else pool,
            failed_parts=(
                self._failed_parts if isinstance(failed_parts, _Unset) else failed_parts
            ),
            playing=self._playing if isinstance(playing, _Unset) else playing,
            last_played=(
                self._last_played if isinstance(last_played, _Unset) else last_played
            ),
            mode=self._mode if isinstance(mode, _Unset) else mode,
            filling=self._filling if isinstance(filling, _Unset) else filling,
            attempts=self._attempts if isinstance(attempts, _Unset) else attempts,
            last_error=(
                self._last_error if isinstance(last_error, _Unset) else last_error
            ),
        )

    # -- value semantics (PY-OP-2) -----------------------------------------

    def _fields(
        self,
    ) -> tuple[
        Format,
        frozenset[Part],
        FrozenParts,
        Part | None,
        Part | None,
        Mode,
        bool,
        int,
        Reason | None,
    ]:
        return (
            self._format,
            self._pool,
            self._failed_parts,
            self._playing,
            self._last_played,
            self._mode,
            self._filling,
            self._attempts,
            self._last_error,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ProgramState):
            return NotImplemented
        return self._fields() == other._fields()

    def __hash__(self) -> int:
        return hash((ProgramState, *self._fields()))

    def __repr__(self) -> str:
        return (
            f"ProgramState(mode={self._mode!s}, pool={len(self._pool)}, "
            f"playing={self._playing!r}, filling={self._filling}, "
            f"attempts={self._attempts}, failed={len(self._failed_parts)})"
        )
