"""The ``SwitchProgram`` control signal -- swap the animated Program atomically.

Turning a Program on or playing a saved one replaces *which* Program the daemon
animates. Doing that swap in the handler thread would be a lost update: a
second client's command could interleave against a half-swapped state. So the
switch is a :class:`ControlSignal` posted to the single :class:`ControlChannel`
writer, which applies it atomically -- retarget the channel to the freshly seeded
Program, point the shared :class:`ActiveContext` at its backing store/directory,
then drive the one transition (``turn_on`` to generate, ``start_from_disk`` to
replay). The prior Program is discarded and its playback stopped (``interrupts``);
the reconcile that follows starts or cancels the fill for the new Program.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from punt_vox.voxd.programs.active_context import ActiveContext, ActiveProgram
    from punt_vox.voxd.programs.control_channel import ControlChannel
    from punt_vox.voxd.programs.part import Part
    from punt_vox.voxd.programs.playback_source import PlaybackSource
    from punt_vox.voxd.programs.program import Program

__all__ = ["SwitchProgram"]


@final
@dataclass(frozen=True, slots=True)
class SwitchProgram:
    """Retarget the daemon to a freshly seeded Program, then start it.

    ``target is None`` starts generation from the seeded pool (``turn_on`` --
    the ``music on`` path); a ``target`` cold-starts replay from disk with no
    fill (``start_from_disk`` -- the ``play <name>`` path). ``program`` is built
    by the service already carrying the disk pool as its state, so the switch
    needs no pool-reseed transition on the entity.
    """

    channel: ControlChannel
    context: ActiveContext
    program: Program
    active: ActiveProgram
    target: Part | None

    @property
    def interrupts(self) -> bool:
        """A switch stops whatever was playing at once and begins the new Program."""
        return True

    def apply(self, _source: PlaybackSource, /) -> bool:
        """Retarget the channel and context, then drive the seeded transition.

        The prior source (the positional argument) is discarded outright -- the
        switch animates the freshly seeded :attr:`program` instead, whether the
        displaced source was a generate Program or a replay Selection.
        """
        self.channel.retarget(self.program)
        self.context.switch(self.active)
        if self.target is None:
            self.program.turn_on()
        else:
            self.program.start_from_disk(self.target)
        return True
