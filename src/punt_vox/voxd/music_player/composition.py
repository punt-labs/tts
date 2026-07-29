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

_RESTART_SECONDS = 5.0


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
        """Project the scene and run both legs, restarting the whole cycle on a fault.

        This is a guarded restart loop mirroring each leg's own pattern one level up.
        Each iteration re-projects the initial scene, then runs the push and receive
        legs together under one :class:`asyncio.TaskGroup`. Every leg self-heals in
        its own guarded loop; but should either ever fail fatally, the TaskGroup
        cancels its sibling and raises, and this loop logs the traceback to the
        persistent daemon log and restarts both legs after a backoff.

        The subsystem is fire-and-forget from ``daemon.py`` (no restart path), so it
        must never re-raise: re-raising would route the fault into a scene task whose
        error is swallowed on shutdown, leaving the whole music player silently dead.
        Restarting instead re-creates both legs -- a fresh subscription reconnects,
        re-registers the menu, re-subscribes, and the re-projection re-pushes the
        scene (register-fresh). Both legs are safely re-runnable: the publisher
        reconnects its REST client lazily and the subscription builds a fresh hub
        listener per connect, so neither re-runs a spent per-connection object.

        Only ``Exception`` is caught, never ``BaseException``: the ``CancelledError``
        raised when the daemon cancels this task on shutdown propagates out of the
        loop, tearing both legs down together and ending the subsystem cleanly.
        """
        while True:
            self._project_initial_scene()
            try:
                async with asyncio.TaskGroup() as legs:
                    legs.create_task(self._publisher.run())
                    legs.create_task(self._subscription.run())
            except Exception:
                logger.exception(
                    "music player: a leg failed fatally; restarting both after backoff"
                )
                await asyncio.sleep(_RESTART_SECONDS)

    def _project_initial_scene(self) -> None:
        """Push the initial scene, guarded so a projection fault never blocks the legs.

        A fault building the first scene is logged, never fatal, so both legs still
        start and a later change signal (or the next restart) re-projects.
        """
        try:
            self._player.notify_changed()
        except Exception:
            logger.exception("music player: initial scene projection failed")
