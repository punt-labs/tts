"""``VoxPanelLeg`` -- the panel's one live Hub connection: clicks and control events.

``punt_lux.applets.AppletLeg`` is ``@final`` and dispatches menu clicks over
its own private ``LuxHubClient``, with no hook to call ``subscribe`` before
``listen`` -- and the design confirmed against lux internals
(``docs/vox-control-panel-ui.md``) is that scene-interaction events must ride
that SAME connection, never a second one under one identity (a second
connection would take the first's menu registration away). This leg is built
from the same public primitives ``AppletLeg`` itself is composed from -- an
identity's REST client and the ``LuxHubClient`` its ``.listener(...)`` builds
-- adding the one thing the sealed wrapper cannot expose: ``subscribe(*topics)``
before ``listen()``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self, final

from punt_lux import HubUnavailableError, OpError
from punt_lux.applets import ClickLatency
from punt_lux.rest_client import LuxRestClient

from punt_vox.panel.hub_outage_log import HubOutageLog

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping

    from punt_lux.domain.hub.client_identity import ClientIdentity
    from punt_lux.operations import Ok

    from punt_vox.panel.ports import PanelRestClient
    from punt_vox.panel.service import VoxPanelService

logger = logging.getLogger(__name__)

__all__ = ["VoxPanelLeg"]

_HUB_RETRY_SECONDS = 2.0


@final
class VoxPanelLeg:
    """A session's live connection to luxd: menu clicks and subscribed events."""

    _identity: ClientIdentity
    _service: VoxPanelService
    _topics: tuple[str, ...]
    _rest_factory: Callable[[], PanelRestClient]
    _tasks: set[asyncio.Task[None]]
    _outage: HubOutageLog
    __slots__ = (
        "_identity",
        "_outage",
        "_rest_factory",
        "_service",
        "_tasks",
        "_topics",
    )

    def __new__(
        cls,
        identity: ClientIdentity,
        service: VoxPanelService,
        *,
        topics: tuple[str, ...],
        rest_factory: Callable[[], PanelRestClient] | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._identity = identity
        self._service = service
        self._topics = topics
        self._rest_factory = rest_factory or (
            lambda: LuxRestClient.for_identity(identity)
        )
        self._tasks = set()
        self._outage = HubOutageLog(logger)
        return self

    async def serve(self) -> None:
        """Hold the leg until cancelled, reaching for luxd again when it drops."""
        while True:
            await self._listen_once()
            await asyncio.sleep(_HUB_RETRY_SECONDS)

    async def _listen_once(self) -> None:
        """Build one connection, subscribe, and listen until it ends; never die."""
        try:
            rest = self._rest_factory()
        except HubUnavailableError:
            self._outage.note("luxd is not running yet; the panel will retry")
            return
        listener = rest.listener(
            on_callback=self._on_callback,
            on_event=self._on_event,
            on_connect=self._register,
        )
        listener.subscribe(*self._topics)
        try:
            await listener.listen()
        except HubUnavailableError:
            self._outage.note("luxd is not running yet; the panel will retry")
        except Exception:
            logger.exception("the panel's listen leg failed; retrying")

    async def _register(self) -> None:
        """Put the ``Vox`` entry in the menu and warm the settings cache."""
        self._outage.clear()
        result = await asyncio.to_thread(self._register_now)
        if isinstance(result, OpError):
            logger.error("the panel's menu entry was refused: %s", result.reason)
            return
        await asyncio.to_thread(self._service.prefetch)

    def _register_now(self) -> Ok | OpError:
        return self._rest_factory().register_callback(
            self._service.callback_id, self._service.label
        )

    async def _on_callback(self, callback_id: str) -> None:
        """Answer a menu click: acknowledge instantly, then push the fresh scene."""
        if callback_id != self._service.callback_id:
            return
        self._start(self._clicked())

    async def _clicked(self) -> None:
        try:
            await asyncio.to_thread(self._serviced)
        except Exception:
            logger.exception("a panel click could not be served; the leg stays up")

    def _serviced(self) -> None:
        latency = ClickLatency(self._service.callback_id)
        rest = self._rest_factory()
        self._service.acknowledge(rest, latency)
        self._service.service(rest, latency)
        latency.report()

    async def _on_event(self, topic: str, payload: Mapping[str, object]) -> None:
        """Apply one subscribed control change, off the loop, and re-push if changed."""
        self._start(self._changed(topic, payload))

    async def _changed(self, topic: str, payload: Mapping[str, object]) -> None:
        try:
            await asyncio.to_thread(self._apply, topic, payload)
        except Exception:
            logger.exception(
                "a panel control event could not be applied: %s %r", topic, payload
            )

    def _apply(self, topic: str, payload: Mapping[str, object]) -> None:
        try:
            changed = self._service.apply_event(topic, payload)
        except (TypeError, ValueError):
            logger.exception(
                "vox-panel: rejected control event on %s: %r", topic, payload
            )
            return
        except OSError:
            logger.exception(
                "vox-panel: could not persist the %s change; correcting the scene",
                topic,
            )
            self._service.recover_from_write_failure(topic)
            changed = True
        if not changed:
            return
        try:
            rest = self._rest_factory()
        except HubUnavailableError:
            self._outage.note("luxd unavailable; dropped the panel re-push")
            return
        self._service.push_scene(rest)

    def _start(self, work: Coroutine[object, object, None]) -> None:
        """Run *work* on this loop, held so it is never collected mid-run."""
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
