"""Tests for LuxScenePublisher: async render, down/slow luxd never blocks.

The mandatory property (design 3.2): a slow or unreachable luxd must not stall
the caller or the event loop, and a lux failure is logged and dropped, never
raised into audio control.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast, final

from punt_lux import HubUnavailableError, LuxClient, OpError, RenderRequest, SceneShown

from punt_vox.voxd.music_player.lux_scene_publisher import LuxScenePublisher

if TYPE_CHECKING:
    import pytest


def _scene(scene_id: str) -> RenderRequest:
    return RenderRequest(scene_id=scene_id, elements=[], title="Music")


@final
class _FakeSceneAccessor:
    """Records every rendered scene and reports success."""

    def __init__(self) -> None:
        self.rendered: list[RenderRequest] = []

    async def show(self, request: RenderRequest) -> SceneShown | OpError:
        self.rendered.append(request)
        return SceneShown(scene_id=request.scene_id)


@final
class _FakeClient:
    """A LuxClient stand-in exposing only the ``scene`` accessor the publisher uses."""

    def __init__(self) -> None:
        self.scene = _FakeSceneAccessor()


@final
class _DownSceneAccessor:
    async def show(self, request: RenderRequest) -> SceneShown | OpError:
        raise HubUnavailableError("luxd is not running")


@final
class _DownClient:
    """Always unreachable -- what a stopped luxd looks like."""

    def __init__(self) -> None:
        self.scene = _DownSceneAccessor()


@final
class _RejectingSceneAccessor:
    async def show(self, request: RenderRequest) -> SceneShown | OpError:
        return OpError(code="rejected", reason="scene refused")


@final
class _RejectingClient:
    def __init__(self) -> None:
        self.scene = _RejectingSceneAccessor()


@final
class _BlockingSceneAccessor:
    """Blocks for a long time inside show -- a slow luxd."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def show(self, request: RenderRequest) -> SceneShown | OpError:
        self.started.set()
        await asyncio.sleep(5.0)  # slow, but async -- the loop keeps ticking
        return SceneShown(scene_id=request.scene_id)


@final
class _BlockingClient:
    def __init__(self) -> None:
        self.scene = _BlockingSceneAccessor()


def _as_client(fake: object) -> LuxClient:
    """Cast a duck-typed publisher stand-in to the LuxClient type the seam wants."""
    return cast("LuxClient", fake)


async def _drain_once(publisher: LuxScenePublisher, *, settle: float = 0.1) -> None:
    task = asyncio.create_task(publisher.run())
    await asyncio.sleep(settle)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_run_renders_the_submitted_scene() -> None:
    client = _FakeClient()
    publisher = LuxScenePublisher(lambda: _as_client(client))
    publisher.submit(_scene("vox.music"))
    await _drain_once(publisher)
    assert [r.scene_id for r in client.scene.rendered] == ["vox.music"]


async def test_run_logs_the_push_with_element_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _FakeClient()
    publisher = LuxScenePublisher(lambda: _as_client(client))
    publisher.submit(_scene("vox.music"))
    with caplog.at_level(logging.INFO):
        await _drain_once(publisher)
    pushed = [
        r
        for r in caplog.records
        if "[lux]" in r.getMessage() and "pushed vox.music scene" in r.getMessage()
    ]
    assert pushed
    assert "0 elements" in pushed[-1].getMessage()  # the empty scene's element count


async def test_submit_neither_connects_nor_renders() -> None:
    connected = False

    def _connect() -> LuxClient:
        nonlocal connected
        connected = True
        return _as_client(_FakeClient())

    LuxScenePublisher(_connect).submit(_scene("vox.music"))  # no run() -> no drain
    assert connected is False  # submit only touches the mailbox


async def test_a_down_lux_is_dropped_then_reconnects() -> None:
    fake = _FakeClient()
    sequence: list[LuxClient] = [_as_client(_DownClient()), _as_client(fake)]
    clients = iter(sequence)
    publisher = LuxScenePublisher(lambda: next(clients))

    task = asyncio.create_task(publisher.run())
    publisher.submit(_scene("first"))
    await asyncio.sleep(0.1)  # drain 1: down -> dropped, client reset
    publisher.submit(_scene("second"))
    await asyncio.sleep(0.1)  # drain 2: reconnect -> rendered
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert [r.scene_id for r in fake.scene.rendered] == ["second"]


async def test_an_op_error_is_logged_at_error_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    publisher = LuxScenePublisher(lambda: _as_client(_RejectingClient()))
    publisher.submit(_scene("vox.music"))
    with caplog.at_level(logging.WARNING):
        await _drain_once(publisher)
    rejected = [
        r
        for r in caplog.records
        if "[lux]" in r.getMessage() and "rejected" in r.getMessage()
    ]
    assert rejected  # logged, never raised
    # A refused scene is a defect, not a down display: it reads at ERROR, distinct
    # from the WARNING a HubUnavailableError (luxd down) logs.
    assert all(r.levelno == logging.ERROR for r in rejected)


async def test_a_slow_render_does_not_block_the_event_loop() -> None:
    client = _BlockingClient()
    publisher = LuxScenePublisher(lambda: _as_client(client))
    publisher.submit(_scene("vox.music"))
    task = asyncio.create_task(publisher.run())

    await asyncio.wait_for(client.scene.started.wait(), timeout=1.0)
    # The render is now awaiting a 5s sleep; the event loop must keep ticking --
    # five quick sleeps complete while the slow render is still stuck.
    ticks = 0
    for _ in range(5):
        await asyncio.sleep(0.01)
        ticks += 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert ticks == 5
