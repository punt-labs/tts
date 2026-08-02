"""The ``program_off`` wire handler -- turn the active Program off."""

from __future__ import annotations

from typing import final

from punt_vox.voxd.programs.command_handler import ProgramCommandHandler

__all__ = ["OffHandler"]


@final
class OffHandler(ProgramCommandHandler):
    """Handle ``program_off``: stop playback and cancel the fill."""

    __slots__ = ()
    _WIRE_TYPE = "program_off"

    def _run(self, _msg: dict[str, object], /) -> None:
        """Turn the active Program off (no fields to parse)."""
        self._service.off()
