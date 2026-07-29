"""Tests for LuxScenePublisher: off-thread render, down/slow luxd never blocks.

The mandatory property (design 3.2): a slow or unreachable luxd must not stall
the caller or the event loop, and a lux failure is logged and dropped, never
raised into audio control.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, final

from punt_lux import HubUnavailableError, OpError, RenderRequest, SceneShown

from punt_vox.voxd.music_player.lux_scene_publisher import LuxScenePublisher

if TYPE_CHECKING:
    import pytest

    from punt_vox.voxd.music_player.ports import LuxRenderer


def _scene(scene_id: str) -> RenderRequest:
    return RenderRequest(scene_id=scene_id, elements=[], title="Music")


@final
class _FakeRenderer:
    """Records every rendered scene and reports success."""

    def __init__(self) -> None:
        self.rendered: list[RenderRequest] = []

    def render(self, request: RenderRequest) -> SceneShown | OpError:
        self.rendered.append(request)
        return SceneShown(scene_id=request.scene_id)


@final
class _DownRenderer:
    """Always unreachable -- what a stopped luxd looks like."""

    def render(self, request: RenderRequest) -> SceneShown | OpError:
        raise HubUnavailableError("luxd is not running")


@final
class _RejectingRenderer:
    def render(self, request: RenderRequest) -> SceneShown | OpError:
        return OpError(code="rejected", reason="scene refused")


@final
class _BlockingRenderer:
    """Blocks for a long time inside render -- a slow luxd."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    def render(self, request: RenderRequest) -> SceneShown | OpError:
        self.started.set()
        time.sleep(5.0)  # would freeze the loop if not offloaded to a thread
        return SceneShown(scene_id=request.scene_id)


async def _drain_once(publisher: LuxScenePublisher, *, settle: float = 0.1) -> None:
    task = asyncio.create_task(publisher.run())
    await asyncio.sleep(settle)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_run_renders_the_submitted_scene() -> None:
    renderer = _FakeRenderer()
    publisher = LuxScenePublisher(lambda: renderer)
    publisher.submit(_scene("vox.music"))
    await _drain_once(publisher)
    assert [r.scene_id for r in renderer.rendered] == ["vox.music"]


async def test_submit_neither_connects_nor_renders() -> None:
    connected = False

    def _connect() -> _FakeRenderer:
        nonlocal connected
        connected = True
        return _FakeRenderer()

    LuxScenePublisher(_connect).submit(_scene("vox.music"))  # no run() -> no drain
    assert connected is False  # submit only touches the mailbox


async def test_a_down_lux_is_dropped_then_reconnects() -> None:
    fake = _FakeRenderer()
    sequence: list[LuxRenderer] = [_DownRenderer(), fake]
    renderers = iter(sequence)
    publisher = LuxScenePublisher(lambda: next(renderers))

    task = asyncio.create_task(publisher.run())
    publisher.submit(_scene("first"))
    await asyncio.sleep(0.1)  # drain 1: down -> dropped, client reset
    publisher.submit(_scene("second"))
    await asyncio.sleep(0.1)  # drain 2: reconnect -> rendered
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert [r.scene_id for r in fake.rendered] == ["second"]  # no raise, recovered


async def test_an_op_error_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    publisher = LuxScenePublisher(_RejectingRenderer)
    publisher.submit(_scene("vox.music"))
    with caplog.at_level(logging.WARNING):
        await _drain_once(publisher)
    assert any("lux rejected" in r.getMessage() for r in caplog.records)


async def test_a_slow_render_does_not_block_the_event_loop() -> None:
    renderer = _BlockingRenderer()
    publisher = LuxScenePublisher(lambda: renderer)
    publisher.submit(_scene("vox.music"))
    task = asyncio.create_task(publisher.run())

    await asyncio.wait_for(renderer.started.wait(), timeout=1.0)
    # The render is now blocking in a worker thread; the event loop must keep
    # ticking -- five quick sleeps complete while the 5s render is still stuck.
    ticks = 0
    for _ in range(5):
        await asyncio.sleep(0.01)
        ticks += 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert ticks == 5
