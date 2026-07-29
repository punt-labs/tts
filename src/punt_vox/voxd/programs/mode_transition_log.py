"""``ModeTransitionLog`` -- one INFO line per generate-Program mode transition.

A replay :class:`~punt_vox.voxd.programs.selection_playback.SelectionPlayback` has
no lifecycle mode, so it contributes no line; only a Program-mode change to a
different Program mode is reported. A radio<->Program switch reads as ``None`` on
one side and is suppressed, never logged as ``music: None -> playing``. The log
remembers the last mode it saw, so the control channel notes each applied command
by handing over the current source, with no before/after bookkeeping of its own.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd.programs.program import Program

if TYPE_CHECKING:
    from punt_vox.voxd.programs.playback_source import PlaybackSource

__all__ = ["ModeTransitionLog"]

logger = logging.getLogger(__name__)


@final
class ModeTransitionLog:
    """Track the active Program's mode and log each transition to a new one."""

    __slots__ = ("_previous",)
    _previous: str | None

    def __new__(cls, source: PlaybackSource) -> Self:
        self = super().__new__(cls)
        self._previous = cls._label(source)
        return self

    def note(self, source: PlaybackSource) -> None:
        """Log a Program mode change since the last note, then remember the new mode."""
        current = self._label(source)
        if (
            self._previous is not None
            and current is not None
            and self._previous != current
        ):
            logger.info("music: %s → %s", self._previous, current)
        self._previous = current

    @staticmethod
    def _label(source: PlaybackSource) -> str | None:
        """Return the source's fine-grained Program mode, or None for a radio."""
        return source.mode.value if isinstance(source, Program) else None
