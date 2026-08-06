"""Project the daemon's active source into a client-facing ``ProgramStatus``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from punt_vox.types_programs.status import ProgramStatus
from punt_vox.types_programs.status_views import NowPlaying
from punt_vox.voxd.programs.program import Program
from punt_vox.voxd.programs.selection_playback import SelectionPlayback

if TYPE_CHECKING:
    from punt_vox.types_programs.playback_fault import PlaybackFault
    from punt_vox.voxd.programs.active_context import ActiveSource
    from punt_vox.voxd.programs.control_channel import ControlChannel
    from punt_vox.voxd.programs.mpv.mpv_supervisor import MpvSupervisor
    from punt_vox.voxd.programs.playback_health import PlaybackHealth

__all__ = ["StatusProjection"]


@final
class StatusProjection:
    """Read the live source and faults into a base ``ProgramStatus`` (pre-pause).

    Split out of :class:`~punt_vox.voxd.programs.service.ProgramService` so the
    "what is playing?" projection is one cohesive responsibility: it reads the
    control channel's current source and the standing faults fresh per call and
    never caches, so a status read always reflects the daemon's true state. The
    service overlays the transport ``paused`` flag on the result.
    """

    __slots__ = ("_channel", "_health", "_supervisor")
    _channel: ControlChannel
    _supervisor: MpvSupervisor
    _health: PlaybackHealth

    def __new__(
        cls,
        channel: ControlChannel,
        supervisor: MpvSupervisor,
        health: PlaybackHealth,
    ) -> Self:
        self = super().__new__(cls)
        self._channel = channel
        self._supervisor = supervisor
        self._health = health
        return self

    def of(self, active: ActiveSource) -> ProgramStatus:
        """Return the active source's base status (before the paused overlay)."""
        source = self._channel.source
        fault = self._fault()
        if isinstance(source, Program):
            return source.to_status(active.name, fault)
        if isinstance(source, SelectionPlayback):
            cursor = self._now_playing(source)
            return ProgramStatus.radio(active.name, cursor, fault)
        return ProgramStatus.idle()

    def _fault(self) -> PlaybackFault | None:
        """Return the standing fault a client sees, process-level first.

        The one persistent mpv process is the more serious failure -- missing,
        crashed, or given up -- so a supervisor fault outranks a per-part track
        error. Either way the fault is surfaced through ``playback_error``, never
        a log-only signal.
        """
        return self._supervisor.fault or self._health.fault

    @staticmethod
    def _now_playing(source: SelectionPlayback) -> NowPlaying | None:
        """Return the replay cursor's "Part N of M" view, or ``None`` when idle.

        ``N`` is the playing track's 1-based position in the selection and ``M``
        is the selection's size, so ``N <= M`` always holds -- the same
        position-of-count contract the generate-Program status uses. The cursor is
        read O(1) from the source, never rescanned over an uncapped selection.
        """
        position = source.position
        if position is None:
            return None
        return NowPlaying(index=position, of=len(source.selection))
