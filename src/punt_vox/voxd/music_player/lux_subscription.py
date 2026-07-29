"""``LuxSubscription`` -- voxd's receive leg: one hub connection, menu, dispatch.

The subscription holds a *single* live connection to luxd (Z model invariant I): it
registers the ``Music`` menu entry, subscribes once to ``music.play`` and
``music.stop``, and then holds the connection open -- the ``LuxHubClient`` reconnects
and re-subscribes internally, so voxd depends on no surviving Hub state (invariant
III, register-fresh). Each inbound event is decoded and applied exactly once
(invariants II, V); the phase-1 change signal then re-pushes the scene. Every handler
is a boundary that logs and drops on any fault, so one bad frame can never drop the
connection.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self, final

from punt_lux import HubUnavailableError

from punt_vox.voxd.music_player.player_events import PlayerEventCodec
from punt_vox.voxd.music_player.wire import MusicTopic

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from punt_lux import CallbackHandler, EventHandler

    from punt_vox.voxd.music_player.command_ports import PlayerCommands
    from punt_vox.voxd.music_player.hub_ports import HubListener, MenuRegistrar
    from punt_vox.voxd.programs.change_listener import ChangeListener

__all__ = ["LuxSubscription"]

logger = logging.getLogger(__name__)

_MENU_CALLBACK_ID = "music"
_MENU_LABEL = "Music"
_RETRY_SECONDS = 5.0


@final
class LuxSubscription:
    """Own voxd's one hub connection, the ``Music`` menu, and event dispatch."""

    __slots__ = ("_codec", "_connect_hub", "_listener", "_menu", "_opener", "_service")
    _service: PlayerCommands
    _opener: ChangeListener
    _menu: MenuRegistrar
    _connect_hub: Callable[[EventHandler, CallbackHandler], HubListener]
    _codec: PlayerEventCodec
    _listener: HubListener | None  # the one connection; None until run opens it

    def __new__(
        cls,
        service: PlayerCommands,
        opener: ChangeListener,
        menu: MenuRegistrar,
        connect_hub: Callable[[EventHandler, CallbackHandler], HubListener],
    ) -> Self:
        self = super().__new__(cls)
        self._service = service
        self._opener = opener
        self._menu = menu
        self._connect_hub = connect_hub
        self._codec = PlayerEventCodec()
        self._listener = None
        return self

    async def run(self) -> None:
        """Register the menu, open the one connection, subscribe, and listen forever.

        ``listen`` returns only when the daemon cancels this task on shutdown; the
        ``LuxHubClient`` reconnects and re-subscribes internally across drops, so a
        single ``subscribe`` here holds for the connection's whole life (invariant I).
        """
        await self._menu.register(_MENU_CALLBACK_ID, _MENU_LABEL)
        listener = await self._open_listener()
        listener.subscribe(MusicTopic.PLAY, MusicTopic.STOP)
        self._listener = listener
        await listener.listen()

    async def _open_listener(self) -> HubListener:
        """Build the one hub connection, retrying while luxd is unreachable.

        The retry only ever *replaces a construction that never connected*, so at
        most one live connection is ever built (invariant I); a late-starting luxd
        is picked up rather than losing the receive leg for the daemon's lifetime.
        """
        while True:
            try:
                return self._connect_hub(self.on_event, self.on_callback)
            except HubUnavailableError:
                logger.warning("luxd down; retrying the music receive leg")
                await asyncio.sleep(_RETRY_SECONDS)

    async def on_event(self, topic: str, payload: Mapping[str, object]) -> None:
        """Decode and apply one inbound event exactly once; never drop the leg.

        The receive boundary: a malformed frame or a playback refusal (unknown or
        empty album) is logged and dropped, so one bad event can never tear down the
        single hub connection (invariants I, II, V). A play applies ``replay_album``
        and a stop applies ``off``; the change signal then re-pushes the scene.
        """
        try:
            self._codec.decode(topic, payload).apply(self._service)
        except Exception:
            logger.exception("dropping music event on %s: %r", topic, payload)

    async def on_callback(self, callback_id: str) -> None:
        """Open (re-push) the music scene when the ``Music`` menu entry is clicked."""
        if callback_id != _MENU_CALLBACK_ID:
            return
        try:
            self._opener.notify_changed()
        except Exception:
            logger.exception("music menu open failed for %r", callback_id)
