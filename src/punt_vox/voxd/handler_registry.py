"""Builds the voxd WebSocket handler dispatch table from wired subsystems."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from punt_vox.cache import cache_clear, cache_status
from punt_vox.logging_config import apply_log_level
from punt_vox.paths import recordings_dir
from punt_vox.voxd.cache_handlers import CacheClearHandler, CacheStatusHandler
from punt_vox.voxd.chimes import ChimeResolver
from punt_vox.voxd.dedup import ChimeDedup, OnceDedup
from punt_vox.voxd.fetch_handler import FetchHandler
from punt_vox.voxd.log_level_handler import LogLevelHandler
from punt_vox.voxd.play_handler import PlayHandler
from punt_vox.voxd.rec_handlers import RecListHandler, RecRemoveHandler
from punt_vox.voxd.record_handler import RecordHandler
from punt_vox.voxd.record_store import RecordStore
from punt_vox.voxd.speech_handlers import SynthesizeHandler
from punt_vox.voxd.system_handlers import ChimeHandler, HealthHandler, VoicesHandler

if TYPE_CHECKING:
    from punt_vox.voxd.health import DaemonHealth
    from punt_vox.voxd.playback import PlaybackQueue
    from punt_vox.voxd.programs.wiring import ProgramSubsystem
    from punt_vox.voxd.synthesis import SynthesisPipeline
    from punt_vox.voxd.types import MessageHandler

__all__ = ["HandlerRegistry"]


@final
class HandlerRegistry:
    """Assemble the ``{message type -> handler}`` dispatch table.

    Extracted from the daemon composition root so wiring the concrete handlers
    (and the dedup/chime collaborators they need) lives in one focused module
    rather than fanning every handler import into the daemon.
    """

    __slots__ = ("_health", "_playback", "_programs", "_synthesis")

    _synthesis: SynthesisPipeline
    _playback: PlaybackQueue
    _programs: ProgramSubsystem
    _health: DaemonHealth

    def __new__(
        cls,
        *,
        synthesis: SynthesisPipeline,
        playback: PlaybackQueue,
        programs: ProgramSubsystem,
        health: DaemonHealth,
    ) -> Self:
        self = super().__new__(cls)
        self._synthesis = synthesis
        self._playback = playback
        self._programs = programs
        self._health = health
        return self

    def build(self) -> dict[str, MessageHandler]:
        """Return the canonical handler dispatch dict (speech + system + programs).

        ``record``, ``play``, and ``fetch`` share one :class:`RecordStore` so the
        containment root and its path checks are defined in exactly one place;
        ``fetch`` also resolves music parts through the programs' catalog library.
        """
        store = RecordStore(recordings_dir())
        return {
            "synthesize": SynthesizeHandler(
                synthesis=self._synthesis,
                playback=self._playback,
                once_dedup=OnceDedup(),
            ),
            "record": RecordHandler(synthesis=self._synthesis, store=store),
            "play": PlayHandler(playback=self._playback, store=store),
            "fetch": FetchHandler(store=store, music=self._programs.library),
            "rec_list": RecListHandler(store),
            "rec_remove": RecRemoveHandler(store),
            "cache_status": CacheStatusHandler(cache_status),
            "cache_clear": CacheClearHandler(cache_clear),
            "set_log_level": LogLevelHandler(apply_log_level),
            "chime": ChimeHandler(
                chimes=ChimeResolver(),
                chime_dedup=ChimeDedup(),
                playback=self._playback,
            ),
            "voices": VoicesHandler(),
            "health": HealthHandler(health=self._health),
            **self._programs.handlers(),
        }
