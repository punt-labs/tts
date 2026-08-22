"""Integration tests for MusicPlayerSubsystem: subscribe, initial push, re-push.

These wire the real ChangeSignal -> MusicPlayer -> LuxScenePublisher chain and prove
the mandatory non-blocking property: emitting a change (as the control channel or
catalog would, on the single-writer) returns at once even when lux is slow. The
receive leg runs alongside with a fake hub listener that fires on_connect on its
handshake, so the initial menu registration and initial scene push ride that hook
(as they do in production) and the subsystem drives both legs as one task without a
running luxd.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, cast, final

from punt_lux.operations import Ok

from punt_vox.types_programs.status import ProgramStatus
from punt_vox.voxd.music_player import composition
from punt_vox.voxd.music_player.composition import MusicPlayerSubsystem
from punt_vox.voxd.programs.change_signal import ChangeSignal

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest
    from punt_lux import (
        CallbackHandler,
        EventHandler,
        LuxClient,
        OpError,
        RenderRequest,
        SceneShown,
    )
    from punt_lux.hub_client import ConnectHandler

    from punt_vox.voxd.music_player.hub_ports import HubListener
    from punt_vox.voxd.music_player.lux_scene_publisher import LuxScenePublisher
    from punt_vox.voxd.music_player.lux_subscription import LuxSubscription
    from punt_vox.voxd.programs.album_id import AlbumId
    from punt_vox.voxd.programs.catalog import Album

    type AlbumFactory = Callable[..., Album]


@final
class _FakeService:
    """A ProgramSeam double: fixed status/catalog plus recorded commands."""

    def __init__(self, status: ProgramStatus, albums: tuple[Album, ...]) -> None:
        self._status = status
        self._albums = albums
        self.played: list[AlbumId] = []
        self.stops = 0

    def status(self) -> ProgramStatus:
        return self._status

    def catalog_albums(self) -> tuple[Album, ...]:
        return self._albums

    def replay_album(self, album_id: AlbumId) -> None:
        self.played.append(album_id)

    def stop(self) -> None:
        self.stops += 1

    def advance(self) -> None:  # pragma: no cover - unused here
        raise NotImplementedError

    def prev(self) -> None:  # pragma: no cover - unused here
        raise NotImplementedError

    def pause(self) -> None:  # pragma: no cover - unused here
        raise NotImplementedError

    def resume(self) -> None:  # pragma: no cover - unused here
        raise NotImplementedError


@final
class _FlakyService:
    """Raise on the first projection, then succeed -- a bad status/catalog read."""

    def __init__(self, status: ProgramStatus, albums: tuple[Album, ...]) -> None:
        self._status = status
        self._albums = albums
        self._calls = 0

    def status(self) -> ProgramStatus:
        return self._status

    def catalog_albums(self) -> tuple[Album, ...]:
        self._calls += 1
        if self._calls == 1:
            msg = "catalog read failed at startup"
            raise RuntimeError(msg)
        return self._albums

    def replay_album(self, album_id: AlbumId) -> None:  # pragma: no cover - unused here
        raise NotImplementedError

    def stop(self) -> None:  # pragma: no cover - unused here
        raise NotImplementedError

    def advance(self) -> None:  # pragma: no cover - unused here
        raise NotImplementedError

    def prev(self) -> None:  # pragma: no cover - unused here
        raise NotImplementedError

    def pause(self) -> None:  # pragma: no cover - unused here
        raise NotImplementedError

    def resume(self) -> None:  # pragma: no cover - unused here
        raise NotImplementedError


@final
class _FakeSceneAccessor:
    def __init__(self, outer: _FakeClient) -> None:
        self._outer = outer

    async def show(self, request: RenderRequest) -> SceneShown | OpError:
        from punt_lux import SceneShown

        self._outer.rendered.append(request)
        return SceneShown(scene_id=request.scene_id)


@final
class _FakeCallbackAccessor:
    def __init__(self, outer: _FakeClient) -> None:
        self._outer = outer

    async def register(self, callback_id: str, label: str) -> Ok | OpError:
        self._outer.menus.append((callback_id, label))
        return Ok()


@final
class _FakeClient:
    """A LuxClient stand-in: records rendered scenes and menu registrations."""

    def __init__(self) -> None:
        self.rendered: list[RenderRequest] = []
        self.menus: list[tuple[str, str]] = []
        self.scene = _FakeSceneAccessor(self)
        self.callback = _FakeCallbackAccessor(self)


@final
class _BlockingSceneAccessor:
    async def show(self, request: RenderRequest) -> SceneShown | OpError:
        from punt_lux import SceneShown

        await asyncio.sleep(5.0)
        return SceneShown(scene_id=request.scene_id)


@final
class _BlockingCallbackAccessor:
    async def register(self, callback_id: str, label: str) -> Ok | OpError:
        return Ok()


@final
class _BlockingClient:
    """A LuxClient stand-in whose show blocks, to prove the writer never waits."""

    def __init__(self) -> None:
        self.scene = _BlockingSceneAccessor()
        self.callback = _BlockingCallbackAccessor()


@final
class _FakeHubListener:
    """A HubListener double: fires on_connect once (a handshake), then blocks.

    Firing on_connect models the real client's per-handshake setup: it is what the
    initial menu registration and initial scene push now ride, so the fake must fire
    it for the subsystem to paint its first scene.
    """

    def __init__(self, on_connect: ConnectHandler) -> None:
        self.subscribed: tuple[str, ...] = ()
        self._on_connect = on_connect
        self._stopped = asyncio.Event()

    def subscribe(self, *topics: str) -> None:
        self.subscribed = self.subscribed + topics

    async def listen(self) -> None:
        result = self._on_connect()  # the handshake fires the app's on_connect
        if result is not None:
            await result
        await self._stopped.wait()

    def stop(self) -> None:
        self._stopped.set()


def _as_client(fake: object) -> LuxClient:
    return cast("LuxClient", fake)


@final
class _FakeClients:
    """A LuxClientFactory double handing out one facade and one hub listener."""

    def __init__(self, client: _FakeClient | _BlockingClient) -> None:
        self._client = client
        self.hub_calls = 0

    def client(self) -> LuxClient:
        return _as_client(self._client)

    def hub(
        self,
        on_event: EventHandler,
        on_callback: CallbackHandler,
        on_connect: ConnectHandler,
    ) -> HubListener:
        self.hub_calls += 1
        return _FakeHubListener(on_connect)


@final
class _CollapsingLeg:
    """A leg that raises once (escaping its own guard), then blocks on re-run.

    The per-leg guards make a fatal escape unreachable in practice; this double
    simulates that defensive case to exercise the subsystem's restart loop. It
    raises on the first ``run`` and blocks on the second so the restart settles.
    """

    def __init__(self) -> None:
        self.runs = 0
        self._blocked = asyncio.Event()

    async def run(self) -> None:
        self.runs += 1
        if self.runs == 1:
            msg = "leg collapsed fatally"
            raise RuntimeError(msg)
        await self._blocked.wait()


@final
class _BlockingLeg:
    """A leg that records each run and blocks until the task is cancelled."""

    def __init__(self) -> None:
        self.runs = 0
        self._blocked = asyncio.Event()

    async def run(self) -> None:
        self.runs += 1
        await self._blocked.wait()


def _inject_legs(
    sub: MusicPlayerSubsystem,
    publisher: _CollapsingLeg | _BlockingLeg,
    subscription: _CollapsingLeg | _BlockingLeg,
) -> None:
    """Replace the subsystem's two legs with test doubles to drive the run loop."""
    sub._publisher = cast("LuxScenePublisher", publisher)
    sub._subscription = cast("LuxSubscription", subscription)


async def _run(sub: MusicPlayerSubsystem, *, settle: float) -> asyncio.Task[None]:
    task = asyncio.create_task(sub.run())
    await asyncio.sleep(settle)
    return task


async def _stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_run_pushes_the_initial_scene_then_re_pushes_on_change(
    album_of: AlbumFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = _FakeService(ProgramStatus.idle(), (album_of("aa11bb", name="Mix"),))
    changes = ChangeSignal()
    client = _FakeClient()
    sub = MusicPlayerSubsystem(service, changes, _FakeClients(client))

    with caplog.at_level(logging.INFO):
        task = await _run(sub, settle=0.1)
        assert len(client.rendered) == 1  # the initial vox.music scene

        changes.emit()  # a state change, as the control channel / catalog fires it
        await asyncio.sleep(0.1)
        assert len(client.rendered) == 2  # re-pushed on the change
        assert client.menus == [("music", "Music")]  # the receive leg registered

        await _stop(task)
    assert all(r.scene_id == "vox.music" for r in client.rendered)
    assert any(
        "[lux]" in r.getMessage() and "starting both lux legs" in r.getMessage()
        for r in caplog.records
    )


async def test_emit_returns_at_once_even_when_lux_is_slow(
    album_of: AlbumFactory,
) -> None:
    service = _FakeService(ProgramStatus.idle(), (album_of("aa11bb"),))
    changes = ChangeSignal()
    sub = MusicPlayerSubsystem(service, changes, _FakeClients(_BlockingClient()))

    task = await _run(sub, settle=0.05)  # the initial push is now blocking in lux
    start = time.monotonic()
    changes.emit()  # the single-writer path: project + submit, never the PUT
    elapsed = time.monotonic() - start

    assert elapsed < 0.1  # did not wait on the 5s render
    await _stop(task)


async def test_a_failing_on_connect_projection_does_not_kill_the_publisher(
    album_of: AlbumFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The initial projection now rides on_connect (fired on the handshake). A failing
    # first read is logged and dropped there, so both legs keep running and a later
    # change re-projects onto the still-live drainer.
    service = _FlakyService(ProgramStatus.idle(), (album_of("aa11bb"),))
    changes = ChangeSignal()
    client = _FakeClient()
    sub = MusicPlayerSubsystem(service, changes, _FakeClients(client))

    with caplog.at_level(logging.ERROR):
        task = await _run(sub, settle=0.1)
        assert client.rendered == []  # the on_connect projection raised -> no push
        assert client.menus == [("music", "Music")]  # menu still registered first

        changes.emit()  # a later change re-projects onto the still-live drainer
        await asyncio.sleep(0.1)
        assert len(client.rendered) == 1  # the publisher survived and drained it

    await _stop(task)
    assert any("scene projection on connect" in r.getMessage() for r in caplog.records)


async def test_a_fatal_leg_fault_restarts_both_legs(
    album_of: AlbumFactory,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(composition, "_RESTART_SECONDS", 0.0)  # no backoff wait
    service = _FakeService(ProgramStatus.idle(), (album_of("aa11bb"),))
    sub = MusicPlayerSubsystem(service, ChangeSignal(), _FakeClients(_FakeClient()))
    publisher = _CollapsingLeg()  # the push leg collapses fatally once
    subscription = _BlockingLeg()  # the sibling the TaskGroup cancels
    _inject_legs(sub, publisher, subscription)

    with caplog.at_level(logging.ERROR):
        task = await _run(sub, settle=0.1)
        # The fatal fault did not permanently stop the subsystem: both legs were
        # re-created, so a fresh subscription reconnects and re-registers.
        assert publisher.runs >= 2
        assert subscription.runs >= 2

    await _stop(task)
    assert any("restarting both" in r.getMessage() for r in caplog.records)


async def test_shutdown_cancellation_exits_the_loop_without_restart(
    album_of: AlbumFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(composition, "_RESTART_SECONDS", 0.0)
    service = _FakeService(ProgramStatus.idle(), (album_of("aa11bb"),))
    sub = MusicPlayerSubsystem(service, ChangeSignal(), _FakeClients(_FakeClient()))
    publisher = _BlockingLeg()
    subscription = _BlockingLeg()
    _inject_legs(sub, publisher, subscription)

    task = await _run(sub, settle=0.05)
    task.cancel()  # the daemon cancels the fire-and-forget scene task on shutdown
    await asyncio.gather(task, return_exceptions=True)

    # CancelledError is a BaseException: it propagates out of the while loop rather
    # than being caught and retried, so the subsystem tears down instead of looping.
    assert task.cancelled()
    assert publisher.runs == 1
    assert subscription.runs == 1
