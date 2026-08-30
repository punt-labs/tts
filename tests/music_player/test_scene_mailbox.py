"""Tests for SceneMailbox: latest-wins one-slot handoff, non-blocking submit.

The install intent is the interesting part. Coalescing is what keeps a slow luxd
from back-pressuring the writer, but a menu click swallowed by that coalescing
would silently become a refresh -- the window the user asked to see would stay
behind whatever it was behind. So the intent is sticky until the drain takes it.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from punt_lux import RenderRequest

from punt_vox.voxd.music_player.scene_mailbox import SceneMailbox


def _scene(scene_id: str) -> RenderRequest:
    return RenderRequest(scene_id=scene_id, elements=[], title="Music")


async def test_get_returns_the_submitted_scene() -> None:
    mailbox = SceneMailbox()
    scene = _scene("vox.music")
    mailbox.submit(scene)
    assert (await mailbox.get()).request is scene


async def test_get_coalesces_to_the_newest_scene() -> None:
    mailbox = SceneMailbox()
    mailbox.submit(_scene("first"))
    newest = _scene("second")
    mailbox.submit(newest)  # a burst of changes collapses to the latest
    assert (await mailbox.get()).request is newest


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
    assert (await mailbox.get()).request.scene_id == "s999"


class TestInstallIntent:
    async def test_a_plain_submit_asks_for_no_install(self) -> None:
        mailbox = SceneMailbox()
        mailbox.submit(_scene("vox.music"))
        assert (await mailbox.get()).install is False

    async def test_reinstall_asks_for_one(self) -> None:
        mailbox = SceneMailbox()
        mailbox.reinstall(_scene("vox.music"))
        assert (await mailbox.get()).install is True

    async def test_refreshes_after_a_click_do_not_swallow_its_intent(self) -> None:
        mailbox = SceneMailbox()
        mailbox.reinstall(_scene("clicked"))
        mailbox.submit(_scene("newest"))  # two change signals land before the drain
        mailbox.submit(_scene("newest"))

        delivery = await mailbox.get()

        assert delivery.install is True  # the click's intent survived
        assert delivery.request.scene_id == "newest"  # ... on the newest scene

    async def test_the_intent_is_cleared_once_taken(self) -> None:
        mailbox = SceneMailbox()
        mailbox.reinstall(_scene("clicked"))
        await mailbox.get()
        mailbox.submit(_scene("refresh"))
        assert (await mailbox.get()).install is False


async def test_get_self_heals_from_a_wake_with_no_scene(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mailbox = SceneMailbox()
    mailbox._ready.set()  # force the "unreachable" wake: event set, no scene stored

    async def _submit_soon() -> None:
        await asyncio.sleep(0.05)
        mailbox.submit(_scene("real"))

    submitter = asyncio.create_task(_submit_soon())
    with caplog.at_level(logging.WARNING):
        delivery = await asyncio.wait_for(mailbox.get(), timeout=1.0)
    await submitter  # join the helper so it is cleaned up before assertions

    assert delivery.request.scene_id == "real"  # recovered, never raised
    assert any("no scene" in r.getMessage() for r in caplog.records)
