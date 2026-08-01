"""How a playing part stopped -- the value the interrupt race hands the loop.

A part can stop several ways, and the playback loop reacts differently to each.
A user interrupt (skip / off / play-a-part / retune switch) means the loop does
not advance -- the next source decides what plays. Otherwise the load's
ended-future resolved with an :class:`~punt_vox.types_programs.mpv_event.EndFileReason`:
``eof`` is a clean natural end (advance); ``error`` is a bad/corrupt file the
loop records observably then advances past (F3); and the synthetic ``crashed``
means mpv died, so the loop replays the current part rather than advancing (I6).
Folding these into one immutable value keeps :class:`InterruptRace` returning
*what happened* and leaves *what to do about it* to the loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import final

from punt_vox.types_programs.mpv_event import EndFileReason

__all__ = ["TrackEnd"]


@final
@dataclass(frozen=True, slots=True)
class TrackEnd:
    """The outcome of one played part: interrupted, or ended with a reason."""

    # True for a user interrupt (skip / off / switch) -- the loop does not advance;
    # reason is then None (no end-file resolved the load, the interrupt won the race).
    interrupted: bool
    # The end-file reason when the load ended on its own; None when interrupted.
    reason: EndFileReason | None

    @property
    def faulted(self) -> bool:
        """Whether a non-interrupted part ended on a bad file (``error``, F3)."""
        return not self.interrupted and self.reason is EndFileReason.ERROR

    @property
    def crashed(self) -> bool:
        """Whether mpv crashed under the load (synthetic ``crashed`` reason, I6)."""
        return not self.interrupted and self.reason is EndFileReason.CRASHED
