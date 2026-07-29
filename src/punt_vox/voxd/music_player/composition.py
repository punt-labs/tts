"""``MusicPlayerSubsystem`` -- the composition seam wiring the player into voxd.

The daemon builds one of these from the ``ProgramService`` seam and its change
signal. It constructs both legs over one explicit app identity (:class:`VoxLuxClients`,
injected so tests drive fakes): the push leg -- :class:`LuxScenePublisher` and the
change-listening :class:`MusicPlayer` -- and the receive leg -- :class:`LuxSubscription`
with its :class:`LuxMenuRegistrar`. Its :meth:`run` pushes the initial scene, then
runs both legs for the daemon's lifetime as one cancellable task; a down luxd never
blocks either.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd.music_player.lux_clients import VoxLuxClients
from punt_vox.voxd.music_player.lux_menu import LuxMenuRegistrar
from punt_vox.voxd.music_player.lux_scene_publisher import LuxScenePublisher
from punt_vox.voxd.music_player.lux_subscription import LuxSubscription
from punt_vox.voxd.music_player.player import MusicPlayer

if TYPE_CHECKING:
    from punt_vox.voxd.music_player.command_ports import ProgramSeam
    from punt_vox.voxd.music_player.hub_ports import LuxClientFactory
    from punt_vox.voxd.programs.change_signal import ChangeSignal

__all__ = ["MusicPlayerSubsystem"]

logger = logging.getLogger(__name__)


@final
class MusicPlayerSubsystem:
    """Own the music player, its lux publisher, and the receive subscription."""

    __slots__ = ("_player", "_publisher", "_subscription")
    _publisher: LuxScenePublisher
    _player: MusicPlayer
    _subscription: LuxSubscription

    def __new__(
        cls,
        service: ProgramSeam,
        changes: ChangeSignal,
        # None means voxd's real app-identity clients; tests inject a fake factory.
        clients: LuxClientFactory | None = None,
    ) -> Self:
        lux = clients if clients is not None else VoxLuxClients()
        self = super().__new__(cls)
        self._publisher = LuxScenePublisher(lux.rest)
        self._player = MusicPlayer(service, self._publisher)
        self._subscription = LuxSubscription(
            service, self._player, LuxMenuRegistrar(lux.rest), lux.hub
        )
        changes.subscribe(self._player)
        return self

    async def run(self) -> None:
        """Push the initial scene, then run the push and receive legs together.

        The initial projection is guarded like the drain loop: a fault building the
        first scene is logged, never fatal, so both legs still start and a later
        change-signal re-projects. Publisher and subscription then run under one
        :class:`asyncio.TaskGroup`: each leg self-heals in its own guarded loop, but
        should either ever fail fatally the TaskGroup cancels the other and re-raises,
        rather than leaving a half-dead subsystem with one orphaned leg. That fatal
        case is logged here, so nothing escapes unrecorded; cancelling this task on
        shutdown tears both legs down together and propagates cleanly.
        """
        try:
            self._player.notify_changed()
        except Exception:
            logger.exception("music player: initial scene projection failed")
        try:
            async with asyncio.TaskGroup() as legs:
                legs.create_task(self._publisher.run())
                legs.create_task(self._subscription.run())
        except Exception:
            logger.exception("music player: a leg failed fatally; both legs torn down")
