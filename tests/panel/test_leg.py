"""Tests for :mod:`punt_vox.panel.leg`."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING, Literal, cast, final

import pytest
from punt_lux import HubUnavailableError, OpError

from punt_vox.client_errors import VoxdProtocolError
from punt_vox.panel.leg import VoxPanelLeg
from punt_vox.panel.topics import PanelTopic

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from punt_lux import RenderRequest, SceneShown
    from punt_lux.domain.hub.client_identity import ClientIdentity
    from punt_lux.hub_client import CallbackHandler, ConnectHandler, EventHandler
    from punt_lux.operations import Ok

    from punt_vox.panel.ports import HubListener

_IDENTITY = cast("ClientIdentity", object())

# Where a fake is asked to fail: the three connection-setup calls the leg makes
# in order, each of which luxd can drop between.
_FailPoint = Literal["listener", "subscribe", "listen"]

# What voxd says when it refuses -- the text the notice has to carry through.
_REFUSAL = "unknown voice 'nope'"

# How long a blocked fake waits before giving up: finite so a gate left shut
# fails its test instead of hanging the suite on an unreachable worker thread.
_GATE_SECONDS = 5.0

# How long _register may take while a warm-up is blocked. Well under
# _GATE_SECONDS, so a warm-up that went back to being awaited inline fails
# here rather than passing slowly.
_HANDSHAKE_SECONDS = 2.0


def _leg_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return only the records the leg itself emitted, dropping punt_lux's."""
    return [r for r in caplog.records if r.name == "punt_vox.panel.leg"]


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

    _fail_at: _FailPoint | None

    def __init__(
        self, *, fail_at: _FailPoint | None = None, error: Exception | None = None
    ) -> None:
        self.subscribed: tuple[str, ...] = ()
        self.listened = False
        self._fail_at = fail_at
        self._error = error if error is not None else HubUnavailableError("down")

    def subscribe(self, *topics: str) -> None:
        if self._fail_at == "subscribe":
            raise self._error
        self.subscribed = topics

    async def listen(self) -> None:
        if self._fail_at == "listen":
            raise self._error
        self.listened = True


def _ok() -> Ok:
    from punt_lux.operations import Ok

    return Ok()


@final
class _FakeRest:
    """A ``PanelRestClient`` double: canned register result, records listeners."""

    _fail_at: _FailPoint | None

    def __init__(
        self,
        *,
        register_result: Ok | OpError | None = None,
        fail_at: _FailPoint | None = None,
        error: Exception | None = None,
    ) -> None:
        self.register_result = register_result if register_result is not None else _ok()
        self.registered: list[tuple[str, str]] = []
        self.rendered_count = 0
        self.listener_built: _FakeListener | None = None
        self._fail_at = fail_at
        self._error = error if error is not None else HubUnavailableError("down")

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
        if self._fail_at == "listener":
            raise self._error
        self.listener_built = _FakeListener(fail_at=self._fail_at, error=self._error)
        return self.listener_built


class _FakeService:
    """A ``VoxPanelService`` double recording its lifecycle calls."""

    def __init__(
        self,
        *,
        raise_on_apply: bool = False,
        raise_on_write: bool = False,
        raise_on_preview: bool = False,
        raise_on_service: bool = False,
        raise_on_prefetch: bool = False,
    ) -> None:
        self.callback_id = "vox-panel"
        self.label = "Vox"
        self.raise_on_prefetch = raise_on_prefetch
        self.prefetch_gate = threading.Event()
        self.prefetch_gate.set()
        self.prefetch_called = False
        self.acknowledged = 0
        self.serviced = 0
        self.applied: list[tuple[str, Mapping[str, object]]] = []
        self.apply_returns = True
        self.raise_on_apply = raise_on_apply
        self.raise_on_write = raise_on_write
        self.raise_on_preview = raise_on_preview
        self.raise_on_service = raise_on_service
        self.pushed = 0
        self.recovered: list[str] = []
        self.rejections: list[str] = []

    def prefetch(self) -> None:
        if self.raise_on_prefetch:
            raise VoxdProtocolError(_REFUSAL)
        # Open unless a test closes it to stand in for a slow voxd. The
        # timeout is a backstop: a gate left shut must fail its test, never
        # hang the suite on a worker thread nobody can reach.
        self.prefetch_gate.wait(_GATE_SECONDS)
        self.prefetch_called = True

    def acknowledge(self, client: object, latency: object) -> None:
        self.acknowledged += 1

    def service(self, client: object, latency: object) -> None:
        if self.raise_on_service:
            raise VoxdProtocolError(_REFUSAL)
        self.serviced += 1

    def apply_event(self, topic: str, payload: Mapping[str, object]) -> bool:
        if self.raise_on_apply:
            msg = "bad payload"
            raise TypeError(msg)
        if self.raise_on_write:
            msg = "disk full"
            raise OSError(msg)
        if self.raise_on_preview:
            raise VoxdProtocolError(_REFUSAL)
        self.applied.append((topic, payload))
        return self.apply_returns

    def push_scene(self, client: object) -> None:
        self.pushed += 1

    def recover_from_write_failure(self, field: str) -> None:
        self.recovered.append(field)

    def note_rejection(self, detail: str) -> None:
        self.rejections.append(detail)


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
        rest = _FakeRest(fail_at="listener")
        leg = VoxPanelLeg(
            _IDENTITY,
            _FakeService(),  # type: ignore[arg-type]
            topics=(),
            rest_factory=lambda: rest,
        )
        await leg._listen_once()  # must not raise

    async def test_hub_unavailable_subscribing_is_swallowed(self) -> None:
        rest = _FakeRest(fail_at="subscribe")
        leg = VoxPanelLeg(
            _IDENTITY,
            _FakeService(),  # type: ignore[arg-type]
            topics=(),
            rest_factory=lambda: rest,
        )
        await leg._listen_once()  # must not raise

    @pytest.mark.parametrize(
        "fail_at", ["connect", "listener", "subscribe", "listen"], ids=str
    )
    async def test_an_unexpected_error_is_logged_and_never_escapes(
        self, fail_at: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Not every failure is luxd being away: a bug in any of the four setup
        # calls must still be logged and swallowed. Escaping here would end
        # serve()'s loop -- no reconnect for the rest of the session, and no
        # log line to say why.
        caplog.set_level(logging.DEBUG, logger="punt_vox.panel.leg")
        boom = RuntimeError("boom")

        def _rest() -> PanelRestClientLike:
            if fail_at == "connect":
                raise boom
            return _FakeRest(fail_at=cast("_FailPoint", fail_at), error=boom)

        leg = VoxPanelLeg(
            _IDENTITY,
            _FakeService(),  # type: ignore[arg-type]
            topics=(),
            rest_factory=_rest,
        )
        await leg._listen_once()  # must not raise
        assert [r.levelno for r in caplog.records] == [logging.ERROR]
        assert caplog.records[0].exc_info is not None


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
        await _wait_until(lambda: service.prefetch_called)

    async def test_the_warm_up_never_holds_the_handshake(self) -> None:
        # on_connect is awaited before the receive loop starts and before the
        # keepalive that holds this session's lease, so a warm-up awaited
        # there would hold both for as long as voxd takes -- costing the
        # session the very menu entry just registered. A voxd that never
        # answers must therefore not keep _register from returning.
        service = _FakeService()
        service.prefetch_gate.clear()
        leg = VoxPanelLeg(
            _IDENTITY,
            service,  # type: ignore[arg-type]
            topics=(),
            rest_factory=_FakeRest,
        )
        async with asyncio.timeout(_HANDSHAKE_SECONDS):
            await leg._register()
        assert service.prefetch_called is False
        service.prefetch_gate.set()
        await _wait_until(lambda: service.prefetch_called)

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

    async def test_a_refused_warm_up_notices_without_opening_the_panel(self) -> None:
        # The third read voxd can refuse, after the click's and the preview's:
        # it must reach the user like those two, but as a held notice only --
        # a push here would open a panel nobody clicked for, showing the
        # pre-read defaults as if they were the session's real settings.
        rest = _FakeRest()
        service = _FakeService(raise_on_prefetch=True)
        leg = VoxPanelLeg(
            _IDENTITY,
            service,  # type: ignore[arg-type]
            topics=(),
            rest_factory=lambda: rest,
        )
        await leg._register()  # must not raise
        await _wait_until(lambda: bool(service.rejections))
        assert service.rejections == [_REFUSAL]
        assert service.pushed == 0
        assert rest.rendered_count == 0

    async def test_a_refused_warm_up_is_logged_at_error_with_a_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="punt_vox.panel.leg")
        leg = VoxPanelLeg(
            _IDENTITY,
            _FakeService(raise_on_prefetch=True),  # type: ignore[arg-type]
            topics=(),
            rest_factory=_FakeRest,
        )
        await leg._register()
        await _wait_until(lambda: bool(_leg_records(caplog)))
        assert [r.levelno for r in _leg_records(caplog)] == [logging.ERROR]
        assert _leg_records(caplog)[0].exc_info is not None

    async def test_a_refused_warm_up_is_logged_as_the_refusal_it_is(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Unguarded this escapes into the hub client's own on_connect
        # isolation, which logs every failure alike as "on_connect callback
        # failed" -- true, and no help at all in naming what refused.
        caplog.set_level(logging.DEBUG, logger="punt_vox.panel.leg")
        leg = VoxPanelLeg(
            _IDENTITY,
            _FakeService(raise_on_prefetch=True),  # type: ignore[arg-type]
            topics=(),
            rest_factory=_FakeRest,
        )
        await leg._register()
        await _wait_until(lambda: bool(_leg_records(caplog)))
        assert [r.getMessage() for r in _leg_records(caplog)] == [
            "vox-panel: voxd refused the settings read on connect"
        ]


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

    async def test_an_unexpected_click_failure_is_logged_at_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="punt_vox.panel.leg")

        def _raise() -> PanelRestClientLike:
            msg = "boom"
            raise RuntimeError(msg)

        leg = VoxPanelLeg(
            _IDENTITY,
            _FakeService(),  # type: ignore[arg-type]
            topics=(),
            rest_factory=_raise,
        )
        await leg._clicked()  # must not raise
        assert [r.levelno for r in caplog.records] == [logging.ERROR]

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


class TestVoxdRejection:
    """voxd answering with a refusal reaches the user, never just the log.

    An unreachable voxd is a transient the next tick retries away; a refusal
    is a real failure -- so both call paths that can meet one turn it into a
    notice and a re-push instead of letting the blanket ``except Exception``
    reduce it to a log line nobody reads.
    """

    async def test_a_refused_preview_shows_a_notice_and_re_pushes(self) -> None:
        rest = _FakeRest()
        service = _FakeService(raise_on_preview=True)
        leg = VoxPanelLeg(
            _IDENTITY,
            service,  # type: ignore[arg-type]
            topics=(),
            rest_factory=lambda: rest,
        )
        await leg._on_event(PanelTopic.VOICE_PREVIEW.value, {})
        await _wait_until(lambda: service.pushed > 0)  # must not raise
        assert service.rejections == [_REFUSAL]
        assert service.pushed == 1

    async def test_a_refused_preview_is_logged_at_error_with_a_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="punt_vox.panel.leg")
        service = _FakeService(raise_on_preview=True)
        leg = VoxPanelLeg(
            _IDENTITY,
            service,  # type: ignore[arg-type]
            topics=(),
            rest_factory=_FakeRest,
        )
        await leg._on_event(PanelTopic.VOICE_PREVIEW.value, {})
        await _wait_until(lambda: service.pushed > 0)
        assert [r.levelno for r in _leg_records(caplog)] == [logging.ERROR]
        assert _leg_records(caplog)[0].exc_info is not None

    async def test_a_refused_click_refresh_shows_a_notice_and_re_pushes(self) -> None:
        # The click's own settings read is the second path a refusal reaches:
        # acknowledge() already put the stale scene up, so the notice only
        # becomes visible if the leg pushes again after catching this.
        rest = _FakeRest()
        service = _FakeService(raise_on_service=True)
        leg = VoxPanelLeg(
            _IDENTITY,
            service,  # type: ignore[arg-type]
            topics=(),
            rest_factory=lambda: rest,
        )
        await leg._clicked()  # must not raise
        assert service.rejections == [_REFUSAL]
        assert service.pushed == 1

    async def test_a_refused_click_refresh_is_logged_at_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="punt_vox.panel.leg")
        leg = VoxPanelLeg(
            _IDENTITY,
            _FakeService(raise_on_service=True),  # type: ignore[arg-type]
            topics=(),
            rest_factory=_FakeRest,
        )
        await leg._clicked()
        # Only the leg's own records: the click also reports its latency, on
        # punt_lux's logger, and that line is not what this test is about.
        assert [r.levelno for r in _leg_records(caplog)] == [logging.ERROR]
        assert _leg_records(caplog)[0].exc_info is not None


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

    async def test_a_click_while_luxd_is_down_escalates_like_every_other_path(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A click arriving mid-outage is the retry loop's business, not an
        # ERROR traceback per click: the throttled second tick proves it went
        # through HubOutageLog rather than the blanket handler.
        caplog.set_level(logging.DEBUG, logger="punt_vox.panel.leg")

        def _raise() -> PanelRestClientLike:
            raise HubUnavailableError("down")

        leg = VoxPanelLeg(
            _IDENTITY,
            _FakeService(),  # type: ignore[arg-type]
            topics=(),
            rest_factory=_raise,
        )
        await leg._clicked()
        await leg._clicked()
        assert [r.levelno for r in caplog.records] == [logging.WARNING, logging.DEBUG]

    async def test_a_successful_connect_clears_the_outage(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # An outage that ended and one that never stopped read alike in a log
        # unless the connect that ended it closes the report: the tick after a
        # good connect must open a fresh outage at WARNING, not restate an old
        # one at DEBUG.
        caplog.set_level(logging.DEBUG, logger="punt_vox.panel.leg")
        rest = _FakeRest()
        luxd_is_down = True

        def _factory() -> PanelRestClientLike:
            if luxd_is_down:
                raise HubUnavailableError("down")
            return rest

        leg = VoxPanelLeg(
            _IDENTITY,
            _FakeService(),  # type: ignore[arg-type]
            topics=(),
            rest_factory=_factory,
        )
        await leg._listen_once()  # the outage opens
        luxd_is_down = False
        await leg._register()  # luxd answered: the outage is over
        caplog.clear()
        luxd_is_down = True
        await leg._listen_once()
        assert [r.levelno for r in _leg_records(caplog)] == [logging.WARNING]


if TYPE_CHECKING:
    from punt_vox.panel.ports import PanelRestClient as PanelRestClientLike
