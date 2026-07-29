"""Tests for SceneMailbox: latest-wins one-slot handoff, non-blocking submit."""

from __future__ import annotations

import asyncio

import pytest
from punt_lux import RenderRequest

from punt_vox.voxd.music_player.scene_mailbox import SceneMailbox


def _scene(scene_id: str) -> RenderRequest:
    return RenderRequest(scene_id=scene_id, elements=[], title="Music")


async def test_get_returns_the_submitted_scene() -> None:
    mailbox = SceneMailbox()
    scene = _scene("vox.music")
    mailbox.submit(scene)
    assert await mailbox.get() is scene


async def test_get_coalesces_to_the_newest_scene() -> None:
    mailbox = SceneMailbox()
    mailbox.submit(_scene("first"))
    newest = _scene("second")
    mailbox.submit(newest)  # a burst of changes collapses to the latest
    assert await mailbox.get() is newest


async def test_get_blocks_until_a_scene_is_submitted() -> None:
    mailbox = SceneMailbox()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(mailbox.get(), timeout=0.05)


async def test_submit_never_blocks_the_caller() -> None:
    mailbox = SceneMailbox()
    # A thousand submits with no drainer must return at once (a stalled push can
    # never back-pressure the single-writer).
    for i in range(1000):
        mailbox.submit(_scene(f"s{i}"))
    assert (await mailbox.get()).scene_id == "s999"
