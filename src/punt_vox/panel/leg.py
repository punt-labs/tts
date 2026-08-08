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

from punt_lux.rest_client import LuxRestClient

from punt_vox.panel.menu_entry import PanelMenuEntry
from punt_vox.panel.panel_guard import PanelGuard
from punt_vox.panel.panel_runner import PanelRunner

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping

    from punt_lux.domain.hub.client_identity import ClientIdentity

    from punt_vox.panel.ports import PanelRestClient
    from punt_vox.panel.service import VoxPanelService

logger = logging.getLogger(__name__)

__all__ = ["VoxPanelLeg"]

_HUB_RETRY_SECONDS = 2.0
_HUB_UNAVAILABLE_MESSAGE = "luxd is not running yet; the panel will retry"


@final
class VoxPanelLeg:
    """A session's live connection to luxd: menu clicks and subscribed events."""

    _service: VoxPanelService
    _topics: tuple[str, ...]
    _rest_factory: Callable[[], PanelRestClient]
    _tasks: set[asyncio.Task[None]]
    _guard: PanelGuard
    _runner: PanelRunner
    _entry: PanelMenuEntry
    __slots__ = (
        "_entry",
        "_guard",
        "_rest_factory",
        "_runner",
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
        self._service = service
        self._topics = topics
        self._rest_factory = rest_factory or (
            lambda: LuxRestClient.for_identity(identity)
        )
        self._tasks = set()
        self._guard = PanelGuard(service, self._rest_factory, logger)
        self._runner = PanelRunner(service, self._rest_factory, self._guard, logger)
        self._entry = PanelMenuEntry(service, self._rest_factory, self._guard, logger)
        return self

    async def serve(self) -> None:
        """Hold the leg until cancelled, reaching for luxd again when it drops."""
        while True:
            await self._listen_once()
            await asyncio.sleep(_HUB_RETRY_SECONDS)

    async def _listen_once(self) -> None:
        """Build one connection, subscribe, and listen until it ends; never die.

        luxd can drop between any two of these four calls -- the leg retries
        every :data:`_HUB_RETRY_SECONDS`, expecting exactly that -- so all
        four share one outage guard. Anything else escaping them is a bug
        rather than an outage: unlogged and unswallowed, it would end
        ``serve()``'s loop and every reconnect with it, for the whole session.
        """
        try:
            with self._guard.outage(_HUB_UNAVAILABLE_MESSAGE):
                rest = self._rest_factory()
                listener = rest.listener(
                    on_callback=self._on_callback,
                    on_event=self._on_event,
                    on_connect=self._register,
                )
                listener.subscribe(*self._topics)
                await listener.listen()
        except Exception:
            logger.exception("the panel's listen leg failed; retrying")

    async def _register(self) -> None:
        """Note that luxd answered, put the menu entry up, and warm up behind it.

        The warm-up starts the moment the entry is up: an entry nobody can
        click yet has nothing to prefetch for, and one that never went up
        never will. :class:`~punt_vox.panel.menu_entry.PanelMenuEntry` owns
        the entry's own failure boundary -- this is ``on_connect``, and the
        hub client swallows what escapes it under a logger of its own.
        """
        self._guard.connected()
        if await self._entry.registered():
            self._start(self._runner.warmed())

    async def _on_callback(self, callback_id: str) -> None:
        """Answer a menu click: acknowledge instantly, then push the fresh scene."""
        if callback_id != self._service.callback_id:
            return
        self._start(self._runner.clicked())

    async def _on_event(self, topic: str, payload: Mapping[str, object]) -> None:
        """Apply one subscribed control change, off the loop, and re-push if changed."""
        self._start(self._runner.changed(topic, payload))

    def _start(self, work: Coroutine[object, object, None]) -> None:
        """Run *work* on this loop, held so it is never collected mid-run."""
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
