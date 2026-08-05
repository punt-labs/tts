"""Fill-outcome control signals -- what the :class:`Filler` posts per Part.

The Filler is mode-agnostic: it produces a Part (or fails) and posts one of
three outcome signals. Each signal's ``apply`` maps the outcome to the right
Program transition *by the Program's current mode*, so all the mode->transition
dispatch (including the resilience states) lives in one typed place rather than
in the loop. A transient outcome drives the modeled retry machine
(``retry_fails`` below the cap, ``retry_exhausted`` at the cap on an empty pool);
an outcome that lands while the Program is ``retrying`` first ``recover``s.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from punt_vox.types_programs.format import MAX_RETRY
from punt_vox.types_programs.mode import Mode
from punt_vox.voxd.programs.fill_outcome import RecoveringFillOutcome
from punt_vox.voxd.programs.guard import GuardViolationError
from punt_vox.voxd.programs.program import Program

if TYPE_CHECKING:
    from punt_vox.types_programs.identifiers import Reason
    from punt_vox.voxd.programs.part import Part
    from punt_vox.voxd.programs.playback_source import PlaybackSource

__all__ = ["PermanentFailure", "Produced", "TransientFailure"]


@final
@dataclass(frozen=True, slots=True)
class Produced(RecoveringFillOutcome):
    """A Part was generated and is ready to join the pool."""

    part: Part

    def _on_generating_first(self, program: Program) -> None:
        program.first_track_ok(self.part)

    def _on_playing_filling(self, program: Program) -> None:
        program.fill_ok(self.part)


@final
@dataclass(frozen=True, slots=True)
class PermanentFailure(RecoveringFillOutcome):
    """A Part hit a permanent generation error (bad prompt / ToS / missing key)."""

    part: Part
    reason: Reason

    def _on_generating_first(self, program: Program) -> None:
        program.first_track_bad_prompt(self.part, self.reason)

    def _on_playing_filling(self, program: Program) -> None:
        program.fill_bad_part(self.part, self.reason)


@final
@dataclass(frozen=True, slots=True)
class TransientFailure:
    """A Part hit a transient generation error (429 / quota / 5xx / timeout)."""

    reason: Reason

    @property
    def interrupts(self) -> bool:
        """A transient backoff pauses generation, never the existing playback."""
        return False

    def apply(self, source: PlaybackSource, /) -> bool:
        """Drive the retry machine by mode, rejecting against a replay Selection.

        A transient outcome landing while a replay Selection is active is a
        benign lost race: the narrow fails and the writer rejects via
        ``GuardViolationError`` (INFO-logged) instead of crashing. A transient
        never interrupts playback, so the return only marks the outcome consumed.
        """
        if not isinstance(source, Program):
            GuardViolationError.reject("transient outcome dropped: replay active")
        program = source
        mode = program.mode
        if mode is Mode.GENERATING_FIRST:
            program.first_track_transient(self.reason)
        elif mode is Mode.PLAYING_FILLING:
            program.fill_transient(self.reason)
        elif mode is Mode.RETRYING:
            self._continue_retry(program)
        # else: drop -- no fill is in flight to fail.
        return True

    def _continue_retry(self, program: Program) -> None:
        """Route the retrying Program to the one valid transition for its state.

        Every reachable ``(attempts, pool)`` in ``retrying`` has exactly one
        legal move: below the cap it counts (``retry_fails``); at the cap it
        either gives up an empty pool (``retry_exhausted`` -> failed) or, on a
        non-empty pool, self-loops indefinitely (``retry_capped``) because a
        non-empty pool never hard-fails. No branch is guard-rejected.
        """
        if program.state.attempts < MAX_RETRY:
            program.retry_fails(self.reason)
        elif len(program.pool) == 0:
            program.retry_exhausted(self.reason)
        else:
            program.retry_capped(self.reason)
