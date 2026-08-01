"""The mpv program-tier backend -- one persistent process over a JSON IPC socket.

The program audio tier (music today; audiobooks and podcasts later) plays each
part by driving one long-lived ``mpv`` process, not by spawning a fresh player
per part. Three collaborators realise that, split along the lifecycle boundary
the design (``docs/mpv-program-player.md``) and the model
(``docs/mpv-program-player.tex``) fix:

* :class:`MpvClient` owns one live connection -- correlated commands and the
  loop's ended-future.
* :class:`MpvSupervisor` owns the process/connection lifecycle -- spawn, connect,
  crash-detect, restart-with-cap -- and never issues ``loadfile``.
* :class:`MpvProgramPlayer` is the loop-facing player built on the current
  connection.

:data:`MPV_MIN_VERSION` is the pinned minimum mpv version the IPC contract rests
on; ``doctor`` imports it to gate an installed mpv.
"""

from __future__ import annotations

from punt_vox.voxd.programs.mpv.mpv_client import MpvClient
from punt_vox.voxd.programs.mpv.mpv_program_player import (
    MpvPlayHandle,
    MpvProgramPlayer,
)
from punt_vox.voxd.programs.mpv.mpv_supervisor import (
    MPV_MIN_VERSION,
    MpvState,
    MpvSupervisor,
)

__all__ = [
    "MPV_MIN_VERSION",
    "MpvClient",
    "MpvPlayHandle",
    "MpvProgramPlayer",
    "MpvState",
    "MpvSupervisor",
]
