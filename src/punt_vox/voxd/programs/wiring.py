"""The daemon's one seam to the Programs domain -- service plus wire handlers.

``ProgramSubsystem`` is the composition seam the daemon holds instead of reaching
into a dozen program modules: it builds the :class:`ProgramService` from the
on-disk store and the ElevenLabs producer and hands out the seven ``program_*``
wire handlers bound to that service. Keeping the wiring here gives the daemon a
single import into the subsystem (PY-DP-10) and keeps the handler roster in
exactly one place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd.programs.filesystem_store import FilesystemProgramStore
from punt_vox.voxd.programs.library import MusicLibrary
from punt_vox.voxd.programs.library_handlers import (
    MusicManifestHandler,
    MusicNewHandler,
    MusicRemoveHandler,
)
from punt_vox.voxd.programs.list_handler import ListHandler
from punt_vox.voxd.programs.next_handler import NextHandler
from punt_vox.voxd.programs.off_handler import OffHandler
from punt_vox.voxd.programs.on_handler import OnHandler
from punt_vox.voxd.programs.select_handler import SelectHandler
from punt_vox.voxd.programs.service import ProgramService
from punt_vox.voxd.programs.sleeper import RealSleeper
from punt_vox.voxd.programs.status_handler import StatusHandler
from punt_vox.voxd.programs.transport_handlers import (
    PauseHandler,
    PrevHandler,
    ResumeHandler,
)

if TYPE_CHECKING:
    from pathlib import Path

    from punt_vox.voxd.programs.producer import Producer
    from punt_vox.voxd.types import MessageHandler

__all__ = ["ProgramSubsystem"]


@final
class ProgramSubsystem:
    """Own the one ProgramService and expose its wire handlers to the daemon."""

    __slots__ = ("_library", "_root", "_service")
    _root: Path
    _service: ProgramService
    _library: MusicLibrary

    def __new__(cls, root: Path, producer: Producer, mpv_socket: Path) -> Self:
        self = super().__new__(cls)
        store = FilesystemProgramStore(root)
        self._root = root
        self._service = ProgramService(producer, store, root, RealSleeper(), mpv_socket)
        # The library shares the service's one catalog and store, so an authored
        # album is instantly listable/playable and a removed one vanishes.
        self._library = MusicLibrary(self._service.catalog, store, root, producer)
        return self

    @property
    def service(self) -> ProgramService:
        """Return the service the daemon runs and the handlers drive."""
        return self._service

    @property
    def library(self) -> MusicLibrary:
        """Return the catalog-authoring seam (also the fetch music-part resolver)."""
        return self._library

    def handlers(self) -> dict[str, MessageHandler]:
        """Return the playback ``program_*`` and catalog ``music_*`` wire handlers."""
        service = self._service
        library = self._library
        return {
            "program_on": OnHandler(service),
            "program_off": OffHandler(service),
            "program_next": NextHandler(service),
            "program_prev": PrevHandler(service),
            "program_pause": PauseHandler(service),
            "program_resume": ResumeHandler(service),
            "program_select": SelectHandler(service),
            "program_list": ListHandler(service),
            "program_status": StatusHandler(service),
            "music_new": MusicNewHandler(library),
            "music_manifest": MusicManifestHandler(library),
            "music_remove": MusicRemoveHandler(
                library, service.active_backing_locators
            ),
        }
