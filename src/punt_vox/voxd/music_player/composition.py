"""``MusicPlayerSubsystem`` -- the composition seam wiring the player into voxd.

The daemon builds one of these from the ``ProgramService`` seam and its change
signal: it constructs the :class:`LuxScenePublisher` over the public
``LuxRestClient`` and the :class:`MusicPlayer`, and subscribes the player to the
signal so every applied command or catalog edit re-projects the scene. Its
:meth:`run` pushes the initial scene once, then drains scene updates to luxd for
the daemon's lifetime -- one background task, cancelled on shutdown like its
siblings. The ``connect`` callable is injected so tests drive it with a fake.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from punt_lux import LuxRestClient

from punt_vox.voxd.music_player.lux_scene_publisher import LuxScenePublisher
from punt_vox.voxd.music_player.player import MusicPlayer

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.voxd.music_player.ports import PlayerService
    from punt_vox.voxd.programs.change_signal import ChangeSignal

__all__ = ["MusicPlayerSubsystem"]


@final
class MusicPlayerSubsystem:
    """Own the music player and its lux publisher, wired to a change signal."""

    __slots__ = ("_player", "_publisher")
    _publisher: LuxScenePublisher
    _player: MusicPlayer

    def __new__(
        cls,
        service: PlayerService,
        changes: ChangeSignal,
        connect: Callable[[], LuxRestClient] = LuxRestClient.connect,
    ) -> Self:
        self = super().__new__(cls)
        self._publisher = LuxScenePublisher(connect)
        self._player = MusicPlayer(service, self._publisher)
        changes.subscribe(self._player)
        return self

    async def run(self) -> None:
        """Push the initial scene, then drain scene updates to luxd forever."""
        self._player.notify_changed()
        await self._publisher.run()
