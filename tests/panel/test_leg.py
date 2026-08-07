"""Tests for :mod:`punt_vox.panel.leg`."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast, final

from punt_lux import HubUnavailableError, OpError

from punt_vox.panel.leg import VoxPanelLeg
from punt_vox.panel.topics import PanelTopic

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import pytest
    from punt_lux import RenderRequest, SceneShown
    from punt_lux.domain.hub.client_identity import ClientIdentity
    from punt_lux.hub_client import CallbackHandler, ConnectHandler, EventHandler
    from punt_lux.operations import Ok

    from punt_vox.panel.ports import HubListener

_IDENTITY = cast("ClientIdentity", object())


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    """Poll *predicate* until it is true -- a ``to_thread`` worker needs real
    wall-clock time to run, not just an event-loop tick."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            msg = "timed out waiting for the background worker to finish"
            raise AssertionError(msg)
        await asyncio.sleep(0.01)


@final
class _FakeListener:
    """A ``HubListener`` double that records subscriptions and never blocks."""

    def __init__(self, *, raise_on_subscribe: bool = False) -> None:
        self.subscribed: tuple[str, ...] = ()
        self.listened = False
        self._raise_on_subscribe = raise_on_subscribe

    def subscribe(self, *topics: str) -> None:
        if self._raise_on_subscribe:
            raise HubUnavailableError("down")
        self.subscribed = topics

    async def listen(self) -> None:
        self.listened = True


def _ok() -> Ok:
    from punt_lux.operations import Ok

    return Ok()


@final
class _FakeRest:
    """A ``PanelRestClient`` double: canned register result, records listeners."""

    def __init__(
        self,
        *,
        register_result: Ok | OpError | None = None,
        raise_on_listener: bool = False,
        raise_on_subscribe: bool = False,
    ) -> None:
        self.register_result = register_result if register_result is not None else _ok()
        self.registered: list[tuple[str, str]] = []
        self.rendered_count = 0
        self.listener_built: _FakeListener | None = None
        self._raise_on_listener = raise_on_listener
        self._raise_on_subscribe = raise_on_subscribe

    def render(self, request: RenderRequest) -> SceneShown | OpError:
        self.rendered_count += 1
        return cast("SceneShown", _ok())

    def register_callback(self, callback_id: str, label: str) -> Ok | OpError:
        self.registered.append((callback_id, label))
        return self.register_result

    def listener(
        self,
        *,
        on_callback: CallbackHandler,
        on_event: EventHandler,
        on_connect: ConnectHandler | None = None,
    ) -> HubListener:
        if self._raise_on_listener:
            raise HubUnavailableError("down")
        self.listener_built = _FakeListener(raise_on_subscribe=self._raise_on_subscribe)
        return self.listener_built


class _FakeService:
    """A ``VoxPanelService`` double recording its lifecycle calls."""

    def __init__(
        self, *, raise_on_apply: bool = False, raise_on_write: bool = False
    ) -> None:
        self.callback_id = "vox-panel"
        self.label = "Vox"
        self.prefetch_called = False
        self.acknowledged = 0
        self.serviced = 0
        self.applied: list[tuple[str, Mapping[str, object]]] = []
        self.apply_returns = True
        self.raise_on_apply = raise_on_apply
        self.raise_on_write = raise_on_write
        self.pushed = 0
        self.recovered: list[str] = []

    def prefetch(self) -> None:
        self.prefetch_called = True

    def acknowledge(self, client: object, latency: object) -> None:
        self.acknowledged += 1

    def service(self, client: object, latency: object) -> None:
        self.serviced += 1

    def apply_event(self, topic: str, payload: Mapping[str, object]) -> bool:
        if self.raise_on_apply:
            msg = "bad payload"
            raise TypeError(msg)
        if self.raise_on_write:
            msg = "disk full"
            raise OSError(msg)
        self.applied.append((topic, payload))
        return self.apply_returns

    def push_scene(self, client: object) -> None:
        self.pushed += 1

    def recover_from_write_failure(self, field: str) -> None:
        self.recovered.append(field)


class TestListenOnce:
    async def test_builds_listener_subscribes_and_listens(self) -> None:
        rest = _FakeRest()
        service = _FakeService()
        leg = VoxPanelLeg(
            _IDENTITY,
            service,  # type: ignore[arg-type]
            topics=(PanelTopic.NOTIFY.value, PanelTopic.VOICE.value),
            rest_factory=lambda: rest,
        )
        await leg._listen_once()
        assert rest.listener_built is not None
        assert rest.listener_built.subscribed == (
            PanelTopic.NOTIFY.value,
            PanelTopic.VOICE.value,
        )
        assert rest.listener_built.listened is True

    async def test_hub_unavailable_at_connect_is_swallowed(self) -> None:
        def _raise() -> PanelRestClientLike:
            raise HubUnavailableError("down")

        leg = VoxPanelLeg(
            _IDENTITY,
            _FakeService(),  # type: ignore[arg-type]
            topics=(),
            rest_factory=_raise,
        )
        await leg._listen_once()

    async def test_hub_unavailable_building_the_listener_is_swallowed(self) -> None:
        # rest.listener(...) sits between the two calls that were already
        # guarded (_rest_factory() and listener.listen()) -- luxd dropping in
        # this exact window is the retry loop's own documented failure mode.
        rest = _FakeRest(raise_on_listener=True)
        leg = VoxPanelLeg(
            _IDENTITY,
            _FakeService(),  # type: ignore[arg-type]
            topics=(),
            rest_factory=lambda: rest,
        )
        await leg._listen_once()  # must not raise

    async def test_hub_unavailable_subscribing_is_swallowed(self) -> None:
        rest = _FakeRest(raise_on_subscribe=True)
        leg = VoxPanelLeg(
            _IDENTITY,
            _FakeService(),  # type: ignore[arg-type]
            topics=(),
            rest_factory=lambda: rest,
        )
        await leg._listen_once()  # must not raise


class TestRegister:
    async def test_success_registers_and_prefetches(self) -> None:
        rest = _FakeRest()
        service = _FakeService()
        leg = VoxPanelLeg(
            _IDENTITY,
            service,  # type: ignore[arg-type]
            topics=(),
            rest_factory=lambda: rest,
        )
        await leg._register()
        assert rest.registered == [("vox-panel", "Vox")]
        assert service.prefetch_called is True

    async def test_refusal_skips_prefetch(self) -> None:
        rest = _FakeRest(register_result=OpError(code="rejected", reason="taken"))
        service = _FakeService()
        leg = VoxPanelLeg(
            _IDENTITY,
            service,  # type: ignore[arg-type]
            topics=(),
            rest_factory=lambda: rest,
        )
        await leg._register()
        assert service.prefetch_called is False


class TestOnCallback:
    async def test_matching_id_services_the_click(self) -> None:
        rest = _FakeRest()
        service = _FakeService()
        leg = VoxPanelLeg(
            _IDENTITY,
            service,  # type: ignore[arg-type]
            topics=(),
            rest_factory=lambda: rest,
        )
        await leg._on_callback("vox-panel")
        await asyncio.sleep(0)  # let the started task run
        assert service.acknowledged == 1
        assert service.serviced == 1

    async def test_mismatched_id_is_ignored(self) -> None:
        service = _FakeService()
        leg = VoxPanelLeg(
            _IDENTITY,
            service,  # type: ignore[arg-type]
            topics=(),
            rest_factory=_FakeRest,
        )
        await leg._on_callback("some-other-callback")
        await asyncio.sleep(0)
        assert service.acknowledged == 0


class TestOnEvent:
    async def test_changed_event_re_pushes_the_scene(self) -> None:
        rest = _FakeRest()
        service = _FakeService()
        leg = VoxPanelLeg(
            _IDENTITY,
            service,  # type: ignore[arg-type]
            topics=(),
            rest_factory=lambda: rest,
        )
        await leg._on_event(PanelTopic.NOTIFY.value, {"value": 1})
        await asyncio.sleep(0)
        assert service.applied == [(PanelTopic.NOTIFY.value, {"value": 1})]
        assert service.pushed == 1

    async def test_unchanged_event_does_not_re_push(self) -> None:
        rest = _FakeRest()
        service = _FakeService()
        service.apply_returns = False
        leg = VoxPanelLeg(
            _IDENTITY,
            service,  # type: ignore[arg-type]
            topics=(),
            rest_factory=lambda: rest,
        )
        await leg._on_event(PanelTopic.VOICE_PREVIEW.value, {})
        await asyncio.sleep(0)
        assert service.pushed == 0

    async def test_rejected_event_never_raises_out_of_the_leg(self) -> None:
        leg = VoxPanelLeg(
            _IDENTITY,
            _FakeService(raise_on_apply=True),  # type: ignore[arg-type]
            topics=(),
            rest_factory=_FakeRest,
        )
        await leg._on_event(PanelTopic.NOTIFY.value, {})
        await asyncio.sleep(0)  # must not raise

    async def test_luxd_down_on_repush_is_swallowed(self) -> None:
        def _raise() -> PanelRestClientLike:
            raise HubUnavailableError("down")

        service = _FakeService()
        leg = VoxPanelLeg(
            _IDENTITY,
            service,  # type: ignore[arg-type]
            topics=(),
            rest_factory=_raise,
        )
        await leg._on_event(PanelTopic.NOTIFY.value, {"value": 0})
        await asyncio.sleep(0)  # must not raise

    async def test_write_failure_is_caught_distinctly_and_corrects_the_scene(
        self,
    ) -> None:
        rest = _FakeRest()
        service = _FakeService(raise_on_write=True)
        leg = VoxPanelLeg(
            _IDENTITY,
            service,  # type: ignore[arg-type]
            topics=(),
            rest_factory=lambda: rest,
        )
        await leg._on_event(PanelTopic.NOTIFY.value, {"value": 0})
        await _wait_until(lambda: service.pushed > 0)  # must not raise
        assert service.recovered == ["notify"]
        assert service.pushed == 1

    async def test_rejected_payload_repushes_the_held_scene_but_does_not_recover(
        self,
    ) -> None:
        rest = _FakeRest()
        service = _FakeService(raise_on_apply=True)
        leg = VoxPanelLeg(
            _IDENTITY,
            service,  # type: ignore[arg-type]
            topics=(),
            rest_factory=lambda: rest,
        )
        await leg._on_event(PanelTopic.NOTIFY.value, {})
        await _wait_until(lambda: service.pushed > 0)  # must not raise
        assert service.recovered == []
        assert service.pushed == 1


class TestOutageLogging:
    """Every hub-unavailable retry path routes through the same escalation."""

    async def test_first_unavailable_tick_logs_at_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="punt_vox.panel.leg")

        def _raise() -> PanelRestClientLike:
            raise HubUnavailableError("down")

        leg = VoxPanelLeg(
            _IDENTITY,
            _FakeService(),  # type: ignore[arg-type]
            topics=(),
            rest_factory=_raise,
        )
        await leg._listen_once()
        assert [r.levelno for r in caplog.records] == [logging.WARNING]

    async def test_a_quick_second_tick_stays_at_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="punt_vox.panel.leg")

        def _raise() -> PanelRestClientLike:
            raise HubUnavailableError("down")

        leg = VoxPanelLeg(
            _IDENTITY,
            _FakeService(),  # type: ignore[arg-type]
            topics=(),
            rest_factory=_raise,
        )
        await leg._listen_once()
        await leg._listen_once()
        assert [r.levelno for r in caplog.records] == [
            logging.WARNING,
            logging.DEBUG,
        ]

    async def test_a_successful_connect_clears_the_outage(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="punt_vox.panel.leg")
        rest = _FakeRest()
        service = _FakeService()
        leg = VoxPanelLeg(
            _IDENTITY,
            service,  # type: ignore[arg-type]
            topics=(),
            rest_factory=lambda: rest,
        )
        leg._outage.note("simulate an ongoing outage")
        await leg._register()
        caplog.clear()
        # A cleared outage logs at WARNING again, not DEBUG, on the next tick.
        leg._outage.note("down again")
        assert [r.levelno for r in caplog.records] == [logging.WARNING]


if TYPE_CHECKING:
    from punt_vox.panel.ports import PanelRestClient as PanelRestClientLike
