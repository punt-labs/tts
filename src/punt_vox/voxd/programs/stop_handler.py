"""The ``program_stop`` wire handler -- halt the active Program."""

from __future__ import annotations

from typing import final

from punt_vox.voxd.programs.command_handler import ProgramCommandHandler

__all__ = ["StopHandler"]


@final
class StopHandler(ProgramCommandHandler):
    """Handle ``program_stop``: stop playback and cancel the fill."""

    __slots__ = ()
    _WIRE_TYPE = "program_stop"

    def _run(self, _msg: dict[str, object], /) -> None:
        """Halt the active Program (no fields to parse)."""
        self._service.stop()
