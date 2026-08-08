"""``PanelRunner`` -- the work the panel's leg starts and never waits on.

Three pieces of work reach the panel: the warm-up behind a handshake, a menu
click, and a subscribed control change. Every one of them blocks -- a voxd
RPC, a config write, a scene push over HTTP -- so every one runs on a worker
thread rather than the leg's loop, which has a session lease to keep alive.
And every one carries its own failure boundary, because the leg starts them
and nothing awaits them: a failure escaping this far would have nowhere left
to go but a task nobody reads.

Holding them here leaves :class:`~punt_vox.panel.leg.VoxPanelLeg` with the
connection and the dispatch, the way ``punt_lux.applets.AppletLeg`` keeps its
own work in ``ServiceRunner``.

The logger is the leg's, injected as
:class:`~punt_vox.panel.panel_guard.PanelGuard` takes it: these lines are
about the panel's one connection, and reading them under a second module name
would split one story across two.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Self, final

from punt_lux.applets import ClickLatency

from punt_vox.panel.topics import PanelTopic

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable, Mapping

    from punt_vox.panel.panel_guard import PanelGuard
    from punt_vox.panel.ports import PanelRestClient
    from punt_vox.panel.service import VoxPanelService

__all__ = ["PanelRunner"]


@final
class PanelRunner:
    """The panel's three pieces of work: the warm-up, a click, a control change."""

    _service: VoxPanelService
    _rest_factory: Callable[[], PanelRestClient]
    _guard: PanelGuard
    _logger: logging.Logger
    __slots__ = ("_guard", "_logger", "_rest_factory", "_service")

    def __new__(
        cls,
        service: VoxPanelService,
        rest_factory: Callable[[], PanelRestClient],
        guard: PanelGuard,
        logger: logging.Logger,
    ) -> Self:
        self = super().__new__(cls)
        self._service = service
        self._rest_factory = rest_factory
        self._guard = guard
        self._logger = logger
        return self

    async def warmed(self) -> None:
        """Read the settings once behind the handshake, never inside it.

        ``on_connect`` is awaited before the receive loop starts and before
        the keepalive that holds this session's lease, so a warm-up awaited
        there would hold both for as long as voxd takes to answer -- and a
        voxd slow enough would cost the session the very menu entry just
        registered. The leg starts this and its handshake goes on without it.

        A refusal becomes a notice and no re-push: nothing is on screen yet
        to correct, and the notice rides the first click's own push.
        """
        try:
            with self._guard.offscreen_rejection("the settings read on connect"):
                await asyncio.to_thread(self._service.prefetch)
        except Exception:
            self._logger.exception("the panel's warm-up failed; the first click waits")

    async def clicked(self) -> None:
        """Serve one menu click off the loop: acknowledge, then push it fresh."""
        try:
            with self._guard.outage("luxd unavailable; dropped a panel click"):
                await asyncio.to_thread(self._serviced)
        except Exception:
            self._logger.exception(
                "a panel click could not be served; the leg stays up"
            )

    async def changed(self, topic: str, payload: Mapping[str, object]) -> None:
        """Apply one subscribed control change off the loop, re-pushing if it took."""
        try:
            await asyncio.to_thread(self._apply, topic, payload)
        except Exception:
            self._logger.exception(
                "a panel control event could not be applied: %s %r", topic, payload
            )

    def _serviced(self) -> None:
        latency = ClickLatency(self._service.callback_id)
        rest = self._rest_factory()
        self._service.acknowledge(rest, latency)
        with self._guard.rejection("the settings read behind a panel click"):
            self._service.service(rest, latency)
        latency.report()

    def _apply(self, topic: str, payload: Mapping[str, object]) -> None:
        with self._guard.rejection(f"a control change on {topic}"):
            if self._applied(topic, payload):
                self._guard.repush()

    def _applied(self, topic: str, payload: Mapping[str, object]) -> bool:
        """Apply one control event; answer whether the scene needs a re-push.

        Every failure handled here answers yes: the widget already shows the
        change optimistically, so the still-true held scene has to go back
        out to snap it back. A refusal from voxd is deliberately not handled
        here -- the :class:`~punt_vox.panel.panel_guard.PanelGuard` rejection
        guard around the caller owns that one.
        """
        try:
            return self._service.apply_event(topic, payload)
        except (TypeError, ValueError):
            self._logger.exception(
                "vox-panel: rejected control event on %s: %r", topic, payload
            )
        except OSError:
            field = PanelTopic(topic).field_name
            self._logger.exception(
                "vox-panel: could not persist the %s change; correcting the scene",
                field,
            )
            self._service.recover_from_write_failure(field)
        return True
