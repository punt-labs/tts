"""Where a paused part resumes -- the two value objects the suspension keeps.

Pause tears the player down (a clean process exit closes the audio device with
no underrun click) and remembers *where* the part was, so resume can re-spawn
the player seeked to that offset. Two small value objects carry that knowledge:

* :class:`LiveTrack` is the timing of the handle the loop is currently racing --
  the part, the seek offset it was spawned at, and the monotonic instant it
  started. It computes the elapsed playback offset on demand.
* :class:`ResumePoint` is what pause froze: the part and the offset to seek to
  when the loop next spawns it. It answers the loop's one question -- *"seek to
  what, for the part I am about to play?"* -- and returns ``0`` for any part
  other than the one paused, so a ``prev``/``next`` that moved the cursor while
  paused starts the newly-cursored part from its beginning.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from punt_vox.voxd.programs.part import Part

__all__ = ["LiveTrack", "ResumePoint"]


@final
@dataclass(frozen=True, slots=True)
class ResumePoint:
    """The part a pause froze and the offset to resume it at."""

    part: Part
    offset_seconds: float

    def offset_for(self, target: Part) -> float:
        """Return the seek offset for ``target``.

        The recorded offset when ``target`` is the very part that was paused;
        ``0`` otherwise, so a part the cursor moved to while paused (``prev`` /
        ``next``) plays from its start rather than a stale offset.
        """
        return self.offset_seconds if target == self.part else 0.0


@final
@dataclass(frozen=True, slots=True)
class LiveTrack:
    """The timing of the handle the loop is racing: part, base offset, start."""

    part: Part
    base_offset: float
    started_at: float

    def resume_point(self, now: float) -> ResumePoint:
        """Freeze the elapsed playback position as a :class:`ResumePoint`.

        The offset is the seek the handle started at plus the wall time it has
        played since -- so a part paused, resumed, and paused again accumulates
        its total elapsed offset across the cycles rather than losing it.
        """
        return ResumePoint(self.part, self.base_offset + (now - self.started_at))
