"""The shared spine of the recovering fill outcomes (Produced, PermanentFailure).

A produced Part and a permanent per-Part failure apply the same way: if the
Program is retrying, ``recover`` first, then dispatch the mode-appropriate
transition for ``generating_first`` or ``playing_filling`` and drop the outcome
in any other mode. ``RecoveringFillOutcome`` owns that spine (and the shared
``interrupts = False``) as a template method; each concrete outcome supplies
only its two mode hooks. A transient outcome drives the retry machine instead,
so it is *not* one of these -- it lives in ``fill_signal`` on its own.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from punt_vox.types_programs.mode import Mode
from punt_vox.voxd.programs.guard import GuardViolationError
from punt_vox.voxd.programs.program import Program

if TYPE_CHECKING:
    from punt_vox.voxd.programs.playback_source import PlaybackSource

__all__ = ["RecoveringFillOutcome"]


class RecoveringFillOutcome(ABC):
    """A fill outcome that recovers a retrying Program, then dispatches by mode.

    A produced Part and a permanent per-Part failure share one spine: narrow the
    active source to a generate ``Program`` (rejecting as a lost race when a
    replay Selection is active); if it is retrying, ``recover``
    first; then apply the mode-appropriate transition for ``generating_first`` or
    ``playing_filling``, and drop the outcome in any other mode (the pool has
    moved on -- off, rotating, failed). Only the two mode hooks differ per
    outcome. A fill outcome never interrupts what is playing.
    """

    __slots__ = ()

    @property
    def interrupts(self) -> bool:
        """A fill outcome joins the pool or records a failure; it never interrupts."""
        return False

    def apply(self, source: PlaybackSource, /) -> bool:
        """Recover a retrying Program, then apply the mode-appropriate transition.

        A fill outcome landing while a replay Selection is active is a benign
        lost race: the ``isinstance`` narrow fails and the writer
        rejects via ``GuardViolationError`` (INFO-logged) instead of crashing. A
        fill outcome never interrupts, so the return only marks it consumed.
        """
        if not isinstance(source, Program):
            GuardViolationError.reject("fill outcome dropped: replay source active")
        program = source
        if program.mode is Mode.RETRYING:
            program.recover()
        match program.mode:
            case Mode.GENERATING_FIRST:
                self._on_generating_first(program)
            case Mode.PLAYING_FILLING:
                self._on_playing_filling(program)
            case _:
                pass  # the pool no longer wants this outcome -- drop it
        return True

    @abstractmethod
    def _on_generating_first(self, program: Program) -> None:
        """Apply this outcome's ``generating_first`` transition."""

    @abstractmethod
    def _on_playing_filling(self, program: Program) -> None:
        """Apply this outcome's ``playing_filling`` transition."""
