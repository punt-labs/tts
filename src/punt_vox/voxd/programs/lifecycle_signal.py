"""Lifecycle control signals: turn on, turn off, and retune (the on/off path).

``TurnOn`` and ``VibeStyleChange`` are generate-path commands: they narrow
``isinstance(source, Program)`` and reject (``TurnOn``) or no-op
(``VibeStyleChange``) against a consume-only Selection. ``TurnOff`` is
source-agnostic user intent: it stops a generate Program keeping its saved pool,
and stops a replay Selection by retargeting the channel to idle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from punt_vox.voxd.programs.guard import GuardViolationError
from punt_vox.voxd.programs.program import Program

if TYPE_CHECKING:
    from punt_vox.voxd.programs.active_context import ActiveContext
    from punt_vox.voxd.programs.control_channel import ControlChannel
    from punt_vox.voxd.programs.part import Part
    from punt_vox.voxd.programs.playback_source import PlaybackSource

__all__ = ["TurnOff", "TurnOn", "VibeStyleChange"]


@final
@dataclass(frozen=True, slots=True)
class TurnOn:
    """Enter the pool from ``off`` (Z ``TurnOn``) -- generate-only."""

    @property
    def interrupts(self) -> bool:
        """Turn-on starts from ``off`` -- there is no playback to interrupt."""
        return False

    def apply(self, source: PlaybackSource, /) -> bool:
        """Apply the turn-on transition, rejecting a non-generate source."""
        if not isinstance(source, Program):
            GuardViolationError.reject("turn_on requires a generate program")
        source.turn_on()
        return True


@final
@dataclass(frozen=True, slots=True)
class TurnOff:
    """Stop playback: a Program keeps its pool; a replay Selection goes idle.

    User intent, valid against either source. A generate Program
    turns off keeping its saved pool (Z ``TurnOff``); a replay Selection has no
    pool of its own, so it stops by retargeting the channel to the idle Program
    and clearing the active context.
    """

    channel: ControlChannel
    context: ActiveContext
    idle: Program

    @property
    def interrupts(self) -> bool:
        """Turn-off stops playback now."""
        return True

    def apply(self, source: PlaybackSource, /) -> bool:
        """Turn a Program off, or retarget a replay Selection to idle."""
        if isinstance(source, Program):
            source.turn_off()
            return True
        self.channel.retarget(self.idle)
        self.context.clear()
        return True


@final
@dataclass(frozen=True, slots=True)
class VibeStyleChange:
    """Retune to a new (vibe, style) key's saved pool (Z ``VibeStyleChange``).

    Generate-only: a replay Selection carries no vibe adaptation, so a vibe
    change routed to it is a deliberate no-op (the Z ``RadioVibeIgnored``), never
    a lost-race reject -- nothing is lost, the signal simply does not apply.
    """

    new_pool: frozenset[Part]

    @property
    def interrupts(self) -> bool:
        """A retune finishes the current track first, then switches pools."""
        return False

    def apply(self, source: PlaybackSource, /) -> bool:
        """Retune a generate Program; ignore the vibe against a replay Selection.

        A retune never interrupts (it finishes the current track), so the return
        value only reports that the command was consumed -- ``True`` either way.
        """
        if isinstance(source, Program):
            source.vibe_style_change(self.new_pool)
        return True
