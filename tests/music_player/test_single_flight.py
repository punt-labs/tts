"""Tests for :mod:`punt_vox.voxd.music_player.single_flight`."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from punt_vox.voxd.music_player.single_flight import SingleFlightRefresh

if TYPE_CHECKING:
    import pytest


async def test_schedule_runs_the_work_in_the_background() -> None:
    ran = asyncio.Event()

    async def _work() -> None:
        ran.set()

    refresher = SingleFlightRefresh()
    refresher.schedule(_work)
    await asyncio.wait_for(ran.wait(), timeout=1.0)


async def test_running_is_false_before_anything_is_scheduled() -> None:
    assert not SingleFlightRefresh().running


async def test_running_is_true_while_a_run_is_in_flight() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def _work() -> None:
        started.set()
        await release.wait()

    refresher = SingleFlightRefresh()
    refresher.schedule(_work)
    await asyncio.wait_for(started.wait(), timeout=1.0)

    assert refresher.running

    release.set()  # let the task finish so the test doesn't leak it


async def test_running_is_false_again_once_the_run_completes() -> None:
    release = asyncio.Event()

    async def _work() -> None:
        await release.wait()

    refresher = SingleFlightRefresh()
    refresher.schedule(_work)
    await asyncio.sleep(0)
    release.set()
    await asyncio.sleep(0.01)

    assert not refresher.running


async def test_a_call_while_running_is_a_no_op() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def _work() -> None:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    refresher = SingleFlightRefresh()
    refresher.schedule(_work)
    await asyncio.wait_for(started.wait(), timeout=1.0)
    refresher.schedule(_work)  # dropped -- the first run is still in flight
    refresher.schedule(_work)

    release.set()
    await asyncio.sleep(0.01)

    assert calls == 1


async def test_a_dropped_call_while_running_logs_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def _work() -> None:
        started.set()
        await release.wait()

    refresher = SingleFlightRefresh()
    refresher.schedule(_work)
    await asyncio.wait_for(started.wait(), timeout=1.0)

    with caplog.at_level(logging.DEBUG):
        refresher.schedule(_work)  # dropped -- the first run is still in flight

    release.set()
    await asyncio.sleep(0.01)

    assert any(
        "dropped" in r.getMessage() and "in flight" in r.getMessage()
        for r in caplog.records
    )


async def test_a_later_call_after_completion_runs_again() -> None:
    calls = 0

    async def _work() -> None:
        nonlocal calls
        calls += 1

    refresher = SingleFlightRefresh()
    refresher.schedule(_work)
    await asyncio.sleep(0.01)
    refresher.schedule(_work)
    await asyncio.sleep(0.01)

    assert calls == 2


async def test_a_raising_work_still_clears_the_guard() -> None:
    async def _boom() -> None:
        msg = "background work failed"
        raise RuntimeError(msg)

    refresher = SingleFlightRefresh()
    refresher.schedule(_boom)
    await asyncio.sleep(0.01)

    assert refresher.running is False
