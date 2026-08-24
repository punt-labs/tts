"""Tests for :class:`WaitCue`'s background chime-repeat and cleanup discipline."""

from __future__ import annotations

import asyncio
import logging

import pytest

from punt_vox.voxd.conversation_mode.wait_cue import WaitCue


class _CountingChime:
    """A ``ChimeFn`` fake that counts calls and can be told to fail."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1


class _FailingChime:
    """A ``ChimeFn`` fake that always raises."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1
        raise RuntimeError("chime playback failed")


async def test_chime_none_is_a_pure_no_op() -> None:
    cue = WaitCue(None)
    async with cue.active():
        pass  # no background task at all


async def test_active_repeats_the_chime_on_the_configured_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "punt_vox.voxd.conversation_mode.wait_cue._WAIT_CHIME_INTERVAL_S", 0.01
    )
    chime = _CountingChime()
    cue = WaitCue(chime)
    async with cue.active():
        await asyncio.sleep(0.035)
    assert chime.calls >= 2


class TestWaitCueChimeFailure:
    """Item 3: a failing chime must not propagate out of the repeat loop and
    must not mask the real turn's own exception racing it in ``active()``'s
    ``finally: await task`` cleanup.
    """

    async def test_a_failing_chime_is_logged_not_raised(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(
            "punt_vox.voxd.conversation_mode.wait_cue._WAIT_CHIME_INTERVAL_S", 0.01
        )
        chime = _FailingChime()
        cue = WaitCue(chime)
        with caplog.at_level(
            logging.ERROR, logger="punt_vox.voxd.conversation_mode.wait_cue"
        ):
            async with cue.active():
                await asyncio.sleep(0.035)  # survives multiple failing chimes
        assert chime.calls >= 2
        assert any("wait cue chime failed" in r.message for r in caplog.records)

    async def test_a_failing_chime_does_not_mask_the_real_turn_s_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The body -- standing in for the real turn's ``send_turn`` -- must
        raise its own exception through ``active()``'s context manager
        untouched, even though the background chime task is also failing
        and being cancelled/awaited in the same ``finally`` block.
        """
        monkeypatch.setattr(
            "punt_vox.voxd.conversation_mode.wait_cue._WAIT_CHIME_INTERVAL_S", 0.01
        )
        chime = _FailingChime()
        cue = WaitCue(chime)
        with pytest.raises(ValueError, match="the real turn failed"):
            async with cue.active():
                await asyncio.sleep(0.035)  # let the chime fail at least once
                msg = "the real turn failed"
                raise ValueError(msg)


class TestWaitCueActiveCleanup:
    """Item 11: ``active()``'s cancel-on-exit guarantee -- the background
    chime task must actually be cancelled and awaited whenever the
    ``async with`` body exits, on both the happy and the raising path.
    """

    async def test_the_background_task_is_cancelled_on_normal_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "punt_vox.voxd.conversation_mode.wait_cue._WAIT_CHIME_INTERVAL_S", 100.0
        )
        chime = _CountingChime()
        cue = WaitCue(chime)
        tasks_before = asyncio.all_tasks()
        async with cue.active():
            spawned = asyncio.all_tasks() - tasks_before
            assert len(spawned) == 1
            (task,) = spawned
            assert not task.done()
        assert task.cancelled()

    async def test_the_background_task_is_cancelled_when_the_body_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "punt_vox.voxd.conversation_mode.wait_cue._WAIT_CHIME_INTERVAL_S", 100.0
        )
        chime = _CountingChime()
        cue = WaitCue(chime)
        tasks_before = asyncio.all_tasks()
        task: asyncio.Task[None] | None = None
        with pytest.raises(RuntimeError, match="body failed"):
            async with cue.active():
                spawned = asyncio.all_tasks() - tasks_before
                (task,) = spawned
                msg = "body failed"
                raise RuntimeError(msg)
        assert task is not None
        assert task.cancelled()
