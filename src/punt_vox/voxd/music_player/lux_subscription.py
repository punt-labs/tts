"""``LuxSubscription`` -- voxd's receive leg: one hub connection, menu, dispatch.

The subscription holds a *single* live connection to luxd at a time (Z model
invariant I). It subscribes to ``music.play`` and ``music.stop`` and holds the
connection open; the ``LuxHubClient`` reconnects and re-subscribes internally across
transient drops, firing the subscription's ``on_connect`` hook after *every*
successful handshake -- first connect and every internal reconnect. That hook
re-registers the ``Music`` menu entry and re-pushes the scene, so a >30s luxd outage
that lapses the menu lease (swept by luxd) is healed the instant the listener rejoins
internally, without waiting for an outer fault (invariant III, register-fresh). A
guarded restart loop still wraps the whole connect/subscribe/listen cycle as a
backstop: a fault the internal reconnect cannot ride out -- a down luxd, or a
protocol frame that fails validation deep inside ``listen`` -- is logged to the
persistent daemon log and the cycle restarts after a backoff, so the receive leg can
never die silently. Each inbound event is decoded and applied exactly once
(invariants II, V). Every handler is a boundary that logs and drops on any fault, so
one bad frame can never drop the connection.
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
    from punt_lux.hub_client import ConnectHandler

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

    __slots__ = ("_codec", "_connect_hub", "_menu", "_opener", "_service")
    _service: PlayerCommands
    _opener: ChangeListener
    _menu: MenuRegistrar
    _connect_hub: Callable[[EventHandler, CallbackHandler, ConnectHandler], HubListener]
    _codec: PlayerEventCodec

    def __new__(
        cls,
        service: PlayerCommands,
        opener: ChangeListener,
        menu: MenuRegistrar,
        connect_hub: Callable[
            [EventHandler, CallbackHandler, ConnectHandler], HubListener
        ],
    ) -> Self:
        self = super().__new__(cls)
        self._service = service
        self._opener = opener
        self._menu = menu
        self._connect_hub = connect_hub
        self._codec = PlayerEventCodec()
        return self

    async def run(self) -> None:
        """Hold voxd's receive leg open, restarting the whole cycle on any fault.

        Each iteration builds one fresh connection, subscribes, and listens; the
        ``Music`` menu registration and the scene re-push ride the ``on_connect``
        hook the hub client fires on every handshake (:meth:`on_connect`), not this
        outer loop. ``listen`` returns only when the daemon requests a stop, so a
        clean return ends the leg. A down luxd is retried with a warning; any other
        fault -- notably a protocol frame that fails validation deep inside
        ``listen`` -- is logged with its traceback to the persistent daemon log and
        the cycle restarts after a backoff, so a transient or protocol error can
        never leave the receive leg silently dead (invariants I, III). Cancellation
        on shutdown is a ``BaseException`` that propagates cleanly out.
        """
        while True:
            try:
                await self._connect_and_listen()
                return
            except HubUnavailableError:
                logger.warning("luxd down; retrying the music receive leg")
                await asyncio.sleep(_RETRY_SECONDS)
            except Exception:
                logger.exception("music receive leg failed; restarting after backoff")
                await asyncio.sleep(_RETRY_SECONDS)

    async def _connect_and_listen(self) -> None:
        """Build one fresh connection, subscribe, and listen.

        The ``Music`` menu registration and the scene re-push no longer live here:
        they ride the ``on_connect`` hook (:meth:`on_connect`) the hub client fires
        after *every* handshake, so an internal reconnect that ``listen`` rides out
        -- the one a >30s outage triggers after luxd sweeps the lease -- re-registers
        without an outer fault (Z model register-fresh, §6.11). At most one live
        connection exists at a time -- a new one is built only after the prior
        ``listen`` has returned or raised (invariant I).
        """
        listener = self._connect_hub(self.on_event, self.on_callback, self.on_connect)
        listener.subscribe(MusicTopic.PLAY, MusicTopic.STOP)
        await listener.listen()

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

    async def on_connect(self) -> None:
        """Re-register the ``Music`` menu and re-push the scene after every handshake.

        The hub client fires this once per successful handshake -- first connect and
        every internal reconnect -- after the ready frame and re-subscribe. A >30s
        luxd outage lapses the menu lease, luxd sweeps the entry, and the internal
        reconnect ``listen`` rides out then fires this to restore it, without waiting
        for an outer fault (register-fresh, invariant III). The registration is
        best-effort and never raises; the scene re-push is guarded here so a transient
        projection failure is logged, not lost, and never skips the registration. lux
        logs-and-continues if this raises, so the session survives regardless.
        """
        await self._menu.register(_MENU_CALLBACK_ID, _MENU_LABEL)
        try:
            self._opener.notify_changed()
        except Exception:
            logger.exception("music scene projection on connect failed")
