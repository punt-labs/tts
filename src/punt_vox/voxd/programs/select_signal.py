"""The ``SwitchSelection`` control signal -- retarget to a consume-only replay.

Playing a Selection (a name, a ``(style, vibe)`` pair, or a style/vibe union)
replaces *which* source the daemon animates with a :class:`SelectionPlayback`.
Like :class:`SwitchProgram`, the swap is a :class:`ControlSignal` posted to the
single :class:`ControlChannel` writer, so it is serialised with every other
mutation. It arms no fill: a Selection is consume-only,
so ``wants_generation`` is false and the reconcile that follows idles the fill.
The seeded :class:`SelectionPlayback` already begins at its first track.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from punt_vox.voxd.programs.active_context import ActiveContext, ActiveSelection
    from punt_vox.voxd.programs.control_channel import ControlChannel
    from punt_vox.voxd.programs.playback_source import PlaybackSource
    from punt_vox.voxd.programs.selection_playback import SelectionPlayback

__all__ = ["SwitchSelection"]


@final
@dataclass(frozen=True, slots=True)
class SwitchSelection:
    """Retarget the daemon to a freshly seeded replay Selection (no fill).

    Verb-parallel to :class:`SwitchProgram`. ``playback`` is the
    consume-only cursor over the resolved Selection; ``active`` is the backing
    context that resolves each Part's opaque locator to a directory under root.
    """

    channel: ControlChannel
    context: ActiveContext
    playback: SelectionPlayback
    active: ActiveSelection

    @property
    def interrupts(self) -> bool:
        """A switch stops whatever was playing at once and begins the replay."""
        return True

    def apply(self, _source: PlaybackSource, /) -> bool:
        """Retarget the channel and context to the seeded replay Selection.

        The prior source (the positional argument) is discarded outright,
        whether it was a generate Program or another replay Selection.
        """
        self.channel.retarget(self.playback)
        self.context.switch(self.active)
        return True
