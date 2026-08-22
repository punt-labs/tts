"""``LuxSubscription`` -- voxd's receive leg: one hub connection, menu, dispatch.

The subscription holds a *single* live connection to luxd at a time (Z model
invariant I). It subscribes to every :class:`MusicTopic` -- the album-list
``music.play`` / ``music.stop`` and the transport bar's ``music.prev`` /
``music.pause`` / ``music.resume`` / ``music.next`` -- and holds the
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

from punt_vox.lux_common import HubOutageLog
from punt_vox.voxd.music_player.lux_trace import LuxTrace
from punt_vox.voxd.music_player.player_event_codec import (
    AnchorUnresolvedError,
    PlayerEventCodec,
)
from punt_vox.voxd.music_player.wire import MusicTopic

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from punt_lux import CallbackHandler, EventHandler
    from punt_lux.hub_client import ConnectHandler

    from punt_vox.voxd.music_player.command_ports import ProgramSeam
    from punt_vox.voxd.music_player.hub_ports import HubListener, MenuRegistrar
    from punt_vox.voxd.music_player.player_events import PlayerEvent
    from punt_vox.voxd.music_player.presenter_ports import ScenePresenter

__all__ = ["LuxSubscription"]

logger = logging.getLogger(__name__)
_trace = LuxTrace(logger)

_MENU_CALLBACK_ID = "music"
_MENU_LABEL = "Music"
_RETRY_SECONDS = 5.0


@final
class LuxSubscription:
    """Own voxd's one hub connection, the ``Music`` menu, and event dispatch."""

    __slots__ = (
        "_codec",
        "_connect_hub",
        "_menu",
        "_outage",
        "_presenter",
        "_service",
    )
    _service: ProgramSeam
    _presenter: ScenePresenter
    _menu: MenuRegistrar
    _connect_hub: Callable[[EventHandler, CallbackHandler, ConnectHandler], HubListener]
    _codec: PlayerEventCodec
    _outage: HubOutageLog

    def __new__(
        cls,
        service: ProgramSeam,
        presenter: ScenePresenter,
        menu: MenuRegistrar,
        connect_hub: Callable[
            [EventHandler, CallbackHandler, ConnectHandler], HubListener
        ],
        # None means the receive leg owns its own outage window; the composition
        # injects a shared instance so the menu registrar's best-effort I/O and
        # this retry loop escalate one continuous outage together.
        outage: HubOutageLog | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._service = service
        self._presenter = presenter
        self._menu = menu
        self._connect_hub = connect_hub
        self._codec = PlayerEventCodec()
        self._outage = outage if outage is not None else HubOutageLog(logger)
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
        attempt = 0
        while True:
            attempt += 1
            try:
                _trace.info("music receive leg connecting (attempt %d)", attempt)
                await self._connect_and_listen()
                _trace.info("music receive leg stopped cleanly")
                return
            except HubUnavailableError:
                # HubOutageLog throttles the retry storm: the first tick lands as
                # WARNING, later ticks as DEBUG, with an INFO restatement every 30s
                # -- so a long luxd outage stays legible without spamming the log.
                self._outage.note(
                    f"[lux] luxd down; retrying the music receive leg in "
                    f"{_RETRY_SECONDS:.1f}s"
                )
                await asyncio.sleep(_RETRY_SECONDS)
            except Exception:
                logger.exception(
                    "[lux] music receive leg failed; restarting in %.1fs (attempt %d)",
                    _RETRY_SECONDS,
                    attempt,
                )
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
        # Subscribe to every topic the scene can publish -- the album-list play/stop
        # AND the transport bar's prev/pause/resume/next -- so a new topic added to
        # MusicTopic is delivered without a second edit here (the bug this replaced:
        # only play/stop were subscribed, so the transport buttons reached no one).
        listener.subscribe(*MusicTopic)
        _trace.info(
            "subscribed to topics %s; listening",
            ", ".join(topic.value for topic in MusicTopic),
        )
        await listener.listen()

    async def on_event(self, topic: str, payload: Mapping[str, object]) -> None:
        """Decode and apply one inbound event exactly once; never drop the leg.

        The receive boundary splits three failure modes. A malformed frame the codec
        cannot decode -- empty payload, unknown topic, non-string anchor -- is logged
        and dropped silently: there is no album to name, so nothing surfaces. A
        well-formed anchor that names no catalogued album (``AnchorUnresolvedError``,
        a vanished album or a stale row-cache click) is logged AND surfaced as a
        transient warning naming the anchor text -- unlike a malformed frame, the
        user clicked something real and the drop must not look silent. A play or
        stop whose ``apply`` is *refused* (the resolved album has no ready tracks)
        is logged AND surfaced by the event itself. Either way one bad event can
        never tear down the single hub connection (invariants I, II, V).
        """
        _trace.info("received %s %r; applying", topic, dict(payload))
        try:
            # Fresh catalog: a music.play anchor resolves against the albums as they
            # stand now, not a subscribe-time snapshot (codec owns the resolution).
            event = self._codec.decode(topic, payload, self._service.catalog_albums())
        except AnchorUnresolvedError as exc:
            # Well-formed click; the album vanished between paint and click.
            logger.warning(
                "[lux] anchor %r on %s names no catalogued album; surfacing",
                exc.anchor,
                topic,
            )
            self._surface_resolve_failure(exc.anchor)
            return
        except Exception:
            logger.exception("[lux] dropping music event on %s: %r", topic, payload)
            return
        self._apply(event)

    def _apply(self, event: PlayerEvent) -> None:
        """Apply the decoded event; on a refusal, log AND surface a scene warning."""
        try:
            event.apply(self._service)
        except Exception:
            logger.exception("[lux] %r could not play; showing it in the scene", event)
            self._surface_failure(event)

    def _surface_failure(self, event: PlayerEvent) -> None:
        """Re-push the scene with the event's own warning; never raise (boundary).

        A refused play/stop left daemon state unchanged, so no change signal fires;
        surfacing the warning is what keeps the failure client-observable.
        """
        try:
            event.surface_failure(self._presenter)
        except Exception:
            logger.exception("[lux] could not surface the playback failure")

    def _surface_resolve_failure(self, anchor: str) -> None:
        """Re-push the scene with a resolve-failure warning; never raise (boundary).

        Mirrors :meth:`_surface_failure` but for the case where nothing resolved:
        the codec produced no event, so the anchor text (the clicked cell) carries
        the message instead of an album id. Failing to surface is logged, not
        propagated -- one bad frame can never tear down the connection.
        """
        try:
            self._presenter.present_resolve_failure(anchor)
        except Exception:
            logger.exception("[lux] could not surface the resolve failure")

    async def on_callback(self, callback_id: str) -> None:
        """Open (re-push) the music scene when the ``Music`` menu entry is clicked."""
        if callback_id != _MENU_CALLBACK_ID:
            return
        _trace.info("Music menu clicked; re-pushing the scene")
        try:
            self._presenter.notify_changed()
        except Exception:
            logger.exception("[lux] music menu open failed for %r", callback_id)

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
        _trace.info("hub handshake complete; re-registering menu and re-pushing scene")
        # The handshake is the ground-truth "luxd is back" signal for both legs,
        # so it closes the shared outage window before the menu register runs.
        self._outage.clear()
        await self._menu.register(_MENU_CALLBACK_ID, _MENU_LABEL)
        try:
            self._presenter.notify_changed()
        except Exception:
            logger.exception("[lux] music scene projection on connect failed")
