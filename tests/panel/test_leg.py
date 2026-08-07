"""Tests for :mod:`punt_vox.panel.leg`."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast, final

from punt_lux import HubUnavailableError, OpError

from punt_vox.panel.leg import VoxPanelLeg
from punt_vox.panel.topics import PanelTopic

if TYPE_CHECKING:
    from collections.abc import Mapping

    from punt_lux import RenderRequest, SceneShown
    from punt_lux.domain.hub.client_identity import ClientIdentity
    from punt_lux.hub_client import CallbackHandler, ConnectHandler, EventHandler
    from punt_lux.operations import Ok

    from punt_vox.panel.ports import HubListener

_IDENTITY = cast("ClientIdentity", object())


@final
class _FakeListener:
    """A ``HubListener`` double that records subscriptions and never blocks."""

    def __init__(self) -> None:
        self.subscribed: tuple[str, ...] = ()
        self.listened = False

    def subscribe(self, *topics: str) -> None:
        self.subscribed = topics

    async def listen(self) -> None:
        self.listened = True


def _ok() -> Ok:
    from punt_lux.operations import Ok

    return Ok()


@final
class _FakeRest:
    """A ``PanelRestClient`` double: canned register result, records listeners."""

    def __init__(self, *, register_result: Ok | OpError | None = None) -> None:
        self.register_result = register_result if register_result is not None else _ok()
        self.registered: list[tuple[str, str]] = []
        self.rendered_count = 0
        self.listener_built: _FakeListener | None = None

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
        self.listener_built = _FakeListener()
        return self.listener_built


class _FakeService:
    """A ``VoxPanelService`` double recording its lifecycle calls."""

    def __init__(self, *, raise_on_apply: bool = False) -> None:
        self.callback_id = "vox-panel"
        self.label = "Vox"
        self.prefetch_called = False
        self.acknowledged = 0
        self.serviced = 0
        self.applied: list[tuple[str, Mapping[str, object]]] = []
        self.apply_returns = True
        self.raise_on_apply = raise_on_apply
        self.pushed = 0

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
        self.applied.append((topic, payload))
        return self.apply_returns

    def push_scene(self, client: object) -> None:
        self.pushed += 1


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


if TYPE_CHECKING:
    from punt_vox.panel.ports import PanelRestClient as PanelRestClientLike
