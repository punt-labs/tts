"""Consume-path control signals: advance, step, play a named Part, cold-start.

``Rotate`` is source-agnostic (Z ``Rotate`` / ``RadioRotate``): it advances
whichever source is active, so it drives a generate Program and a replay Selection
alike -- it is the loop's *end-of-part* auto-advance (Z ``AutoAdvance``, wraps at the
end). ``StepForward``/``StepBack`` are the *user's* transport next/prev (Z ``Next``/
``Prev``): on a replay Selection they walk the ordered pool by one and stall at the
boundary, distinct from the wrapping ``Rotate`` (Z Fork C). ``PlayPart`` and
``StartFromDisk`` are generate-only: they narrow ``isinstance(source, Program)`` and
reject as a lost race against a Selection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from punt_vox.voxd.programs.guard import GuardViolationError
from punt_vox.voxd.programs.program import Program
from punt_vox.voxd.programs.selection_playback import SelectionPlayback

if TYPE_CHECKING:
    from punt_vox.voxd.programs.part import Part
    from punt_vox.voxd.programs.playback_source import PlaybackSource

__all__ = ["PlayPart", "Rotate", "StartFromDisk", "StepBack", "StepForward"]


@final
@dataclass(frozen=True, slots=True)
class Rotate:
    """Advance to another Part (Z ``Rotate`` = ``RadioRotate`` = skip = next = end)."""

    @property
    def interrupts(self) -> bool:
        """A user skip acts now; the loop's own track-end advance sees no player."""
        return True

    def apply(self, source: PlaybackSource, /) -> bool:
        """Advance whichever source is active (generate Program or replay Selection)."""
        source.rotate()
        return True


@final
@dataclass(frozen=True, slots=True)
class StepForward:
    """User transport next: step a replay cursor forward, or skip a Program.

    On a replay Selection it walks the ordered pool by one and stalls at the last
    part (Z ``Next``); on a generate Program there is no ordered position, so it
    falls back to the Program's shuffle skip (``rotate``), preserving the existing
    ``next`` behaviour for a running radio.
    """

    @property
    def interrupts(self) -> bool:
        """A user next acts now: the loop kills the current track and plays anew."""
        return True

    def apply(self, source: PlaybackSource, /) -> bool:
        """Step a replay Selection forward; skip a generate Program.

        Return whether playback moved: a step that stalls at the last slot is a
        no-op (``False``), so the single writer leaves the current track playing.
        """
        if isinstance(source, SelectionPlayback):
            return source.step_forward()
        source.rotate()
        return True


@final
@dataclass(frozen=True, slots=True)
class StepBack:
    """User transport prev: step a replay cursor back (Selection-only, Z ``Prev``).

    Prev is an ordered-pool notion, so it is defined only for a replay Selection; a
    generate Program has no previous position, so a prev against one is rejected as
    a lost race (swallowed by the single writer), never a shuffle.
    """

    @property
    def interrupts(self) -> bool:
        """A user prev acts now: the loop kills the current track and plays anew."""
        return True

    def apply(self, source: PlaybackSource, /) -> bool:
        """Step a replay Selection back, rejecting a generate Program.

        Return whether playback moved: a step that stalls at the first slot is a
        no-op (``False``), so the single writer leaves the current track playing.
        """
        if not isinstance(source, SelectionPlayback):
            GuardViolationError.reject("prev requires a replay selection")
        return source.step_back()


@final
@dataclass(frozen=True, slots=True)
class PlayPart:
    """Play a specific ready Part by name, without anti-repeat (Z ``PlayPart``)."""

    target: Part

    @property
    def interrupts(self) -> bool:
        """Playing a named Part starts it now."""
        return True

    def apply(self, source: PlaybackSource, /) -> bool:
        """Play a named Part on a generate Program, rejecting a replay Selection."""
        if not isinstance(source, Program):
            GuardViolationError.reject("play_part requires a generate program")
        source.play_part(self.target)
        return True


@final
@dataclass(frozen=True, slots=True)
class StartFromDisk:
    """Cold-start playback from a saved pool with no fill (Z ``StartFromDisk``)."""

    target: Part

    @property
    def interrupts(self) -> bool:
        """Cold-start begins from ``off`` -- there is no playback to interrupt."""
        return False

    def apply(self, source: PlaybackSource, /) -> bool:
        """Cold-start a generate Program, rejecting a replay Selection."""
        if not isinstance(source, Program):
            GuardViolationError.reject("start_from_disk requires a generate program")
        source.start_from_disk(self.target)
        return True
