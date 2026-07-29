"""Integration tests for MusicPlayerSubsystem: subscribe, initial push, re-push.

These wire the real ChangeSignal -> MusicPlayer -> LuxScenePublisher chain and prove
the mandatory non-blocking property: emitting a change (as the control channel or
catalog would, on the single-writer) returns at once even when lux is slow. The
receive leg runs alongside with a fake hub listener, so the subsystem drives both
legs as one task without a running luxd.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, final

from punt_lux.operations import Ok

from punt_vox.types_programs.status import ProgramStatus
from punt_vox.voxd.music_player.composition import MusicPlayerSubsystem
from punt_vox.voxd.programs.change_signal import ChangeSignal

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest
    from punt_lux import (
        CallbackHandler,
        EventHandler,
        OpError,
        RenderRequest,
        SceneShown,
    )

    from punt_vox.voxd.music_player.hub_ports import HubListener
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

    def off(self) -> None:
        self.stops += 1


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

    def off(self) -> None:  # pragma: no cover - unused here
        raise NotImplementedError


@final
class _FakeRenderer:
    """A LuxClient double: records rendered scenes and menu registrations."""

    def __init__(self) -> None:
        self.rendered: list[RenderRequest] = []
        self.menus: list[tuple[str, str]] = []

    def render(self, request: RenderRequest) -> SceneShown | OpError:
        from punt_lux import SceneShown

        self.rendered.append(request)
        return SceneShown(scene_id=request.scene_id)

    def register_callback(self, callback_id: str, label: str) -> Ok | OpError:
        self.menus.append((callback_id, label))
        return Ok()


@final
class _BlockingRenderer:
    """A LuxClient double whose render blocks, to prove the writer never waits."""

    def render(self, request: RenderRequest) -> SceneShown | OpError:
        from punt_lux import SceneShown

        time.sleep(5.0)
        return SceneShown(scene_id=request.scene_id)

    def register_callback(self, callback_id: str, label: str) -> Ok | OpError:
        return Ok()


@final
class _FakeHubListener:
    """A HubListener double: records the one subscription, blocks until stopped."""

    def __init__(self) -> None:
        self.subscribed: tuple[str, ...] = ()
        self._stopped = asyncio.Event()

    def subscribe(self, *topics: str) -> None:
        self.subscribed = self.subscribed + topics

    async def listen(self) -> None:
        await self._stopped.wait()

    def stop(self) -> None:
        self._stopped.set()


@final
class _FakeClients:
    """A LuxClientFactory double handing out one renderer and one hub listener."""

    def __init__(self, renderer: _FakeRenderer | _BlockingRenderer) -> None:
        self._renderer = renderer
        self.hub_calls = 0
        self.listener = _FakeHubListener()

    def rest(self) -> _FakeRenderer | _BlockingRenderer:
        return self._renderer

    def hub(self, on_event: EventHandler, on_callback: CallbackHandler) -> HubListener:
        self.hub_calls += 1
        return self.listener


async def _run(sub: MusicPlayerSubsystem, *, settle: float) -> asyncio.Task[None]:
    task = asyncio.create_task(sub.run())
    await asyncio.sleep(settle)
    return task


async def _stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_run_pushes_the_initial_scene_then_re_pushes_on_change(
    album_of: AlbumFactory,
) -> None:
    service = _FakeService(ProgramStatus.idle(), (album_of("aa11bb", name="Mix"),))
    changes = ChangeSignal()
    renderer = _FakeRenderer()
    sub = MusicPlayerSubsystem(service, changes, _FakeClients(renderer))

    task = await _run(sub, settle=0.1)
    assert len(renderer.rendered) == 1  # the initial vox.music scene

    changes.emit()  # a state change, as the control channel / catalog fires it
    await asyncio.sleep(0.1)
    assert len(renderer.rendered) == 2  # re-pushed on the change
    assert renderer.menus == [("music", "Music")]  # the receive leg registered once

    await _stop(task)
    assert all(r.scene_id == "vox.music" for r in renderer.rendered)


async def test_emit_returns_at_once_even_when_lux_is_slow(
    album_of: AlbumFactory,
) -> None:
    service = _FakeService(ProgramStatus.idle(), (album_of("aa11bb"),))
    changes = ChangeSignal()
    sub = MusicPlayerSubsystem(service, changes, _FakeClients(_BlockingRenderer()))

    task = await _run(sub, settle=0.05)  # the initial push is now blocking in lux
    start = time.monotonic()
    changes.emit()  # the single-writer path: project + submit, never the PUT
    elapsed = time.monotonic() - start

    assert elapsed < 0.1  # did not wait on the 5s render
    await _stop(task)


async def test_a_failing_initial_projection_does_not_kill_the_publisher(
    album_of: AlbumFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = _FlakyService(ProgramStatus.idle(), (album_of("aa11bb"),))
    changes = ChangeSignal()
    renderer = _FakeRenderer()
    sub = MusicPlayerSubsystem(service, changes, _FakeClients(renderer))

    with caplog.at_level(logging.ERROR):
        task = await _run(sub, settle=0.1)
        assert renderer.rendered == []  # the initial projection raised -> no push

        changes.emit()  # a later change re-projects onto the still-live drainer
        await asyncio.sleep(0.1)
        assert len(renderer.rendered) == 1  # the publisher survived and drained it

    await _stop(task)
    assert any("initial scene projection" in r.getMessage() for r in caplog.records)
