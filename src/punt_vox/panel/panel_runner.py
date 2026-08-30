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
from typing import TYPE_CHECKING, Self, assert_never, final

from punt_lux.applets import ClickLatency

from punt_vox.panel.control_push import ControlPush
from punt_vox.panel.topics import PanelTopic
from punt_vox.types_errors import ConfigValueError

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable, Mapping

    from punt_lux import LuxClient

    from punt_vox.panel.panel_guard import PanelGuard
    from punt_vox.panel.service import VoxPanelService

__all__ = ["PanelRunner"]


@final
class PanelRunner:
    """The panel's three pieces of work: the warm-up, a click, a control change."""

    _service: VoxPanelService
    _rest_factory: Callable[[], LuxClient]
    _guard: PanelGuard
    _logger: logging.Logger
    __slots__ = ("_guard", "_logger", "_rest_factory", "_service")

    def __new__(
        cls,
        service: VoxPanelService,
        rest_factory: Callable[[], LuxClient],
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
        """Serve one menu click: acknowledge over the facade, then push it fresh."""
        try:
            with self._guard.outage("luxd unavailable; dropped a panel click"):
                await self._serviced()
        except Exception:
            self._logger.exception(
                "a panel click could not be served; the leg stays up"
            )

    async def changed(self, topic: str, payload: Mapping[str, object]) -> None:
        """Apply one subscribed control change, re-pushing if it took."""
        try:
            await self._apply(topic, payload)
        except Exception:
            self._logger.exception(
                "a panel control event could not be applied: %s %r", topic, payload
            )

    async def _serviced(self) -> None:
        latency = ClickLatency(self._service.callback_id)
        rest = self._rest_factory()
        await self._service.acknowledge(rest, latency)
        async with self._guard.rejection("the settings read behind a panel click"):
            await self._service.service(rest, latency)
        latency.report()

    async def _apply(self, topic: str, payload: Mapping[str, object]) -> None:
        async with self._guard.control_rejection(f"a control change on {topic}"):
            match await asyncio.to_thread(self._applied, topic, payload):
                case ControlPush.REFRESH:
                    await self._guard.repush()
                case ControlPush.CORRECT:
                    await self._guard.correct()
                case ControlPush.NONE:
                    pass
                case _ as unreachable:
                    # A future ControlPush member with no case above must
                    # fail loudly here rather than silently no-op -- the
                    # match is exhaustive over today's three members only.
                    assert_never(unreachable)

    def _applied(self, topic: str, payload: Mapping[str, object]) -> ControlPush:
        """Apply one control event; answer what kind of re-push it needs.

        Every failure handled here answers :attr:`~ControlPush.CORRECT`: the
        widget already shows the change optimistically, and a diff against the
        last render this session successfully pushed sees nothing to fix --
        only a full reinstall (see
        :meth:`~punt_vox.panel.panel_push.PanelPush.correct`) snaps it back. A
        refusal from voxd is deliberately not handled here -- the
        :class:`~punt_vox.panel.panel_guard.PanelGuard` rejection guard around
        the caller owns that one, and answers the same way.

        Order matters between the two buckets: ``ConfigValueError`` is a
        ``ValueError``, so catching it second would file a change the user
        really chose as a malformed event and revert it with no notice.

        Both buckets name the topic to the user through
        :attr:`~punt_vox.panel.topics.PanelTopic.label`, never the wire
        value: "that vox.model change was refused" reads out an identifier
        that means nothing outside this codebase. Each resolves the topic
        *inside* its own handler rather than once above the ``try``, so a
        topic this panel does not own still reaches ``apply_event``, which
        logs it and answers :attr:`~ControlPush.NONE`. Resolving first
        turned that handled case into an unhandled one -- ``PanelTopic``
        refuses the unknown value, and the refusal escapes both handlers.
        """
        try:
            changed = self._service.apply_event(topic, payload)
        except (ConfigValueError, OSError):
            control = PanelTopic(topic)
            self._logger.exception(
                "vox-panel: the %s change did not stick; correcting the scene",
                control.label,
            )
            self._recover(control)
            return ControlPush.CORRECT
        except (TypeError, ValueError):
            self._logger.exception(
                "vox-panel: rejected control event on %s: %r", topic, payload
            )
            # The widget already moved to the value the user picked, and the
            # correction below snaps it back. Saying nothing while that
            # happens is the worst of both: the setting visibly reverts and
            # the only account of why is a daemon log the user cannot read.
            self._service.note_control_rejected(PanelTopic(topic).label)
            return ControlPush.CORRECT
        return changed

    def _recover(self, control: PanelTopic) -> None:
        """Re-sync from the real settings after *control*'s config write failed.

        Only a topic that commits a field has a field to revert to. A
        preview reaches here through its own fresh read of the store, so a
        malformed config faults it with nothing written and nothing to
        recover -- asking for "the field that did not stick" would raise
        while handling the failure and strand the widget with no notice at
        all.
        """
        if control.writes_field:
            self._service.recover_from_write_failure(control.field_name)
        else:
            self._service.note_control_rejected(control.label)
