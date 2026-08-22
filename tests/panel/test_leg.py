"""Tests for :mod:`punt_vox.panel.leg`.

The leg's own job: hold one connection, retry it, register the menu entry, and
start the work a click or a control event asks for. What that work then does is
:mod:`punt_vox.panel.panel_runner`'s, and is tested there.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

import pytest
from punt_lux import HubUnavailableError, OpError

from panel.doubles import (
    PANEL_LOGGER,
    FailPoint,
    FakeRest,
    FakeService,
    panel_records,
    wait_until,
)
from punt_vox.panel.topics import PanelTopic

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_lux import LuxClient

    from punt_vox.panel.leg import VoxPanelLeg

# How long _register may take while a warm-up is blocked. Well under the gate
# the double waits on, so a warm-up that went back to being awaited inline
# fails here rather than passing slowly.
_HANDSHAKE_SECONDS = 2.0


def _luxd_is_down() -> LuxClient:
    raise HubUnavailableError("down")


class TestListenOnce:
    async def test_builds_listener_subscribes_and_listens(
        self, build_leg: Callable[..., VoxPanelLeg]
    ) -> None:
        rest = FakeRest()
        leg = build_leg(
            FakeService(),
            lambda: rest,
            topics=(PanelTopic.NOTIFY.value, PanelTopic.VOICE.value),
        )
        await leg._listen_once()
        assert rest.listener_built is not None
        assert rest.listener_built.subscribed == (
            PanelTopic.NOTIFY.value,
            PanelTopic.VOICE.value,
        )
        assert rest.listener_built.listened is True

    async def test_hub_unavailable_at_connect_is_swallowed(
        self, build_leg: Callable[..., VoxPanelLeg]
    ) -> None:
        leg = build_leg(FakeService(), _luxd_is_down)
        await leg._listen_once()

    async def test_hub_unavailable_building_the_listener_is_swallowed(
        self, build_leg: Callable[..., VoxPanelLeg]
    ) -> None:
        # rest.listener(...) sits between the two calls that were already
        # guarded (_rest_factory() and listener.listen()) -- luxd dropping in
        # this exact window is the retry loop's own documented failure mode.
        rest = FakeRest(fail_at="listener")
        leg = build_leg(FakeService(), lambda: rest)
        await leg._listen_once()  # must not raise

    async def test_hub_unavailable_subscribing_is_swallowed(
        self, build_leg: Callable[..., VoxPanelLeg]
    ) -> None:
        rest = FakeRest(fail_at="subscribe")
        leg = build_leg(FakeService(), lambda: rest)
        await leg._listen_once()  # must not raise

    @pytest.mark.parametrize(
        "fail_at", ["connect", "listener", "subscribe", "listen"], ids=str
    )
    async def test_an_unexpected_error_is_logged_and_never_escapes(
        self,
        fail_at: str,
        caplog: pytest.LogCaptureFixture,
        build_leg: Callable[..., VoxPanelLeg],
    ) -> None:
        # Not every failure is luxd being away: a bug in any of the four setup
        # calls must still be logged and swallowed. Escaping here would end
        # serve()'s loop -- no reconnect for the rest of the session, and no
        # log line to say why.
        caplog.set_level(logging.DEBUG, logger=PANEL_LOGGER.name)
        boom = RuntimeError("boom")

        def _rest() -> LuxClient:
            if fail_at == "connect":
                raise boom
            built = FakeRest(fail_at=cast("FailPoint", fail_at), error=boom)
            return cast("LuxClient", built)

        leg = build_leg(FakeService(), _rest)
        await leg._listen_once()  # must not raise
        assert [r.levelno for r in caplog.records] == [logging.ERROR]
        assert caplog.records[0].exc_info is not None


class TestRegister:
    async def test_success_registers_and_starts_the_warm_up(
        self, build_leg: Callable[..., VoxPanelLeg]
    ) -> None:
        rest = FakeRest()
        service = FakeService()
        leg = build_leg(service, lambda: rest)
        await leg._register()
        assert rest.registered == [("vox-panel", "Vox")]
        await wait_until(lambda: service.prefetch_called)

    async def test_the_warm_up_never_holds_the_handshake(
        self, build_leg: Callable[..., VoxPanelLeg]
    ) -> None:
        # on_connect is awaited before the receive loop starts and before the
        # keepalive that holds this session's lease, so a warm-up awaited
        # there would hold both for as long as voxd takes -- costing the
        # session the very menu entry just registered. A voxd that never
        # answers must therefore not keep _register from returning.
        service = FakeService()
        service.prefetch_gate.clear()
        leg = build_leg(service, FakeRest)
        async with asyncio.timeout(_HANDSHAKE_SECONDS):
            await leg._register()
        assert service.prefetch_called is False
        service.prefetch_gate.set()
        await wait_until(lambda: service.prefetch_called)

    async def test_refusal_skips_the_warm_up(
        self, build_leg: Callable[..., VoxPanelLeg]
    ) -> None:
        rest = FakeRest(register_result=OpError(code="rejected", reason="taken"))
        service = FakeService()
        leg = build_leg(service, lambda: rest)
        await leg._register()
        assert service.prefetch_called is False

    @pytest.mark.parametrize(
        "error", [HubUnavailableError("down"), RuntimeError("boom")], ids=type
    )
    async def test_a_raising_registration_never_escapes_on_connect(
        self, error: Exception, build_leg: Callable[..., VoxPanelLeg]
    ) -> None:
        # _register IS on_connect, and the hub client awaits it inside a
        # blanket handler that deliberately leaves the socket up: an escape
        # here is logged under punt_lux's name and never reaches the panel's
        # own guard, while serve()'s retry -- which only fires when the
        # connection ends -- stays unfired. The menu would carry no entry for
        # the rest of the session with nothing in the panel's log to say why.
        rest = FakeRest(fail_at="register", error=error)
        service = FakeService()
        leg = build_leg(service, lambda: rest)
        await leg._register()  # must not raise
        await asyncio.sleep(0)
        assert service.prefetch_called is False


class TestOnCallback:
    async def test_matching_id_starts_the_click(
        self, build_leg: Callable[..., VoxPanelLeg]
    ) -> None:
        rest = FakeRest()
        service = FakeService()
        leg = build_leg(service, lambda: rest)
        await leg._on_callback("vox-panel")
        await wait_until(lambda: service.serviced > 0)
        assert service.acknowledged == 1
        assert service.serviced == 1

    async def test_mismatched_id_is_ignored(
        self, build_leg: Callable[..., VoxPanelLeg]
    ) -> None:
        service = FakeService()
        leg = build_leg(service, FakeRest)
        await leg._on_callback("some-other-callback")
        await asyncio.sleep(0)
        assert service.acknowledged == 0


class TestOnEvent:
    async def test_a_subscribed_event_starts_the_control_change(
        self, build_leg: Callable[..., VoxPanelLeg]
    ) -> None:
        rest = FakeRest()
        service = FakeService()
        leg = build_leg(service, lambda: rest)
        await leg._on_event(PanelTopic.NOTIFY.value, {"value": 1})
        await wait_until(lambda: bool(service.applied))
        assert service.applied == [(PanelTopic.NOTIFY.value, {"value": 1})]


class TestOutageLogging:
    """Every hub-unavailable retry path routes through the same escalation."""

    async def test_first_unavailable_tick_logs_at_warning(
        self, caplog: pytest.LogCaptureFixture, build_leg: Callable[..., VoxPanelLeg]
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=PANEL_LOGGER.name)
        leg = build_leg(FakeService(), _luxd_is_down)
        await leg._listen_once()
        assert [r.levelno for r in caplog.records] == [logging.WARNING]

    async def test_a_quick_second_tick_stays_at_debug(
        self, caplog: pytest.LogCaptureFixture, build_leg: Callable[..., VoxPanelLeg]
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=PANEL_LOGGER.name)
        leg = build_leg(FakeService(), _luxd_is_down)
        await leg._listen_once()
        await leg._listen_once()
        assert [r.levelno for r in caplog.records] == [
            logging.WARNING,
            logging.DEBUG,
        ]

    async def test_a_successful_connect_clears_the_outage(
        self, caplog: pytest.LogCaptureFixture, build_leg: Callable[..., VoxPanelLeg]
    ) -> None:
        # An outage that ended and one that never stopped read alike in a log
        # unless the connect that ended it closes the report: the tick after a
        # good connect must open a fresh outage at WARNING, not restate an old
        # one at DEBUG.
        caplog.set_level(logging.DEBUG, logger=PANEL_LOGGER.name)
        rest = FakeRest()
        luxd_is_down = True

        def _factory() -> LuxClient:
            if luxd_is_down:
                raise HubUnavailableError("down")
            return cast("LuxClient", rest)

        leg = build_leg(FakeService(), _factory)
        await leg._listen_once()  # the outage opens
        luxd_is_down = False
        await leg._register()  # luxd answered: the outage is over
        caplog.clear()
        luxd_is_down = True
        await leg._listen_once()
        assert [r.levelno for r in panel_records(caplog)] == [logging.WARNING]
