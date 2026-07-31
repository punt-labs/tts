"""The consume-only replay source -- a rotation cursor over a fixed Selection.

``SelectionPlayback`` is the replay half of :class:`PlaybackSource` (the Z
``Radio``): a shuffle-rotation over a :class:`Selection` that may span albums,
with no cap, no fill, and no generation. Anti-repeat is delegated to the injected
:class:`PlaybackPolicy` -- the same :class:`RotatePolicy` that backs
``Program.rotate`` -- so "no immediate repeat" is defined once. It
generates nothing: :attr:`wants_generation` is structurally ``False``.
"""

from __future__ import annotations

from typing import Self, final

from punt_vox.voxd.programs.part import Part
from punt_vox.voxd.programs.playback_policy import Advance, PlaybackPolicy
from punt_vox.voxd.programs.selection import Selection

__all__ = ["SelectionPlayback"]


@final
class SelectionPlayback:
    """A cursor over a Selection: begins at the first track, rotates on advance."""

    __slots__ = ("_last_played", "_playing", "_policy", "_position", "_selection")
    _selection: Selection
    _policy: PlaybackPolicy
    _playing: Part | None
    _last_played: Part | None
    # The playing track's 0-based index in the ordered pool; None when idle/empty.
    # Maintained on each rotate so ``position`` is O(1) over an uncapped selection.
    _position: int | None

    def __new__(cls, selection: Selection, policy: PlaybackPolicy) -> Self:
        self = super().__new__(cls)
        self._selection = selection
        self._policy = policy
        pool = selection.playable_pool()
        self._playing = pool[0] if pool else None
        self._position = 0 if pool else None
        self._last_played = None
        return self

    @property
    def selection(self) -> Selection:
        """Return the Selection this cursor replays."""
        return self._selection

    @property
    def playing(self) -> Part | None:
        """Return the (selection-unique) Part currently playing, or ``None``."""
        return self._playing

    @property
    def position(self) -> int | None:
        """Return the playing track's 1-based position in the selection, or ``None``.

        O(1): the cursor is maintained on each rotate, so a status read never
        rescans the (uncapped, possibly cross-library) selection. ``None`` when
        idle or empty, mirroring :attr:`playing`.
        """
        return None if self._position is None else self._position + 1

    @property
    def last_played(self) -> Part | None:
        """Return the Part played immediately before, or ``None``."""
        return self._last_played

    def rotate(self) -> None:
        """Advance to another member of the Selection (auto-advance, next, skip).

        Reuses the injected policy's anti-repeat rule; a singleton selection
        replays its sole member. An empty selection is a caught boundary -- the
        cursor holds ``None`` and rotate is a no-op, never a crash (mirrors the
        empty-pool guard).
        """
        pool = self._selection.playable_pool()
        if not pool:
            return
        result = self._policy.next_part(pool, self._playing)
        if not isinstance(result, Advance):
            msg = "selection policy signalled COMPLETE, which a radio has no end for"
            raise AssertionError(msg)
        self._last_played = self._playing
        self._playing = result.part
        # One index scan here (rotate is infrequent -- once per track/skip) keeps
        # the status read O(1).
        self._position = pool.index(result.part)

    def step_forward(self) -> None:
        """Move the cursor to the next part, capped at the last (Z ``Next``).

        Deterministic, unlike :meth:`rotate`: the user's transport ``next`` walks
        the ordered pool by one and *stalls* at the last part (a no-op), rather than
        wrapping the way the loop's end-of-part :meth:`rotate` does (Z Fork C). An
        empty pool holds no cursor, so it is a no-op.
        """
        self._step(+1)

    def step_back(self) -> None:
        """Move the cursor to the previous part, floored at the first (Z ``Prev``).

        Deterministic: walks the ordered pool back by one and stalls at the first
        part (a no-op). An empty pool holds no cursor, so it is a no-op.
        """
        self._step(-1)

    def _step(self, delta: int) -> None:
        """Move the cursor by ``delta`` within ``[0, len-1]``; a boundary is a no-op."""
        pool = self._selection.playable_pool()
        if self._position is None or not pool:
            return
        target = self._position + delta
        if not (0 <= target < len(pool)):
            return  # a boundary step (Prev at 1, Next at M) is the modelled no-op
        self._last_played = self._playing
        self._position = target
        self._playing = pool[target]

    @property
    def wants_generation(self) -> bool:
        """A Selection never generates -- structurally ``False``."""
        return False

    @property
    def advances_on_end(self) -> bool:
        """Whether the loop should auto-advance this radio on a track end."""
        return self._playing is not None
