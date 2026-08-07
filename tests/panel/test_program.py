"""Tests for :mod:`punt_vox.panel.program`."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, final

from punt_vox.panel.program import VoxPanelProgram

if TYPE_CHECKING:
    import pytest


@final
class _FakeClaim:
    def __init__(self, *, taken: bool) -> None:
        self._taken = taken

    def take(self) -> bool:
        return self._taken


@final
class _FakeLeg:
    def __init__(self) -> None:
        self.serve_started = False
        self.cancelled = False

    async def serve(self) -> None:
        self.serve_started = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@final
class _CrashingLeg:
    """A leg whose serve() ends with a genuine bug, not a cancellation."""

    async def serve(self) -> None:
        msg = "boom"
        raise RuntimeError(msg)


@final
class _FakeWatch:
    def __init__(self) -> None:
        self._ended = asyncio.Event()

    def end(self) -> None:
        self._ended.set()

    async def until_session_ends(self) -> None:
        await self._ended.wait()


class TestRun:
    async def test_refused_claim_never_starts_the_leg(self) -> None:
        leg = _FakeLeg()
        program = VoxPanelProgram(_FakeClaim(taken=False), leg, _FakeWatch())  # type: ignore[arg-type]
        await program.run()
        assert leg.serve_started is False

    async def test_taken_claim_serves_until_the_session_ends(self) -> None:
        leg = _FakeLeg()
        watch = _FakeWatch()
        program = VoxPanelProgram(_FakeClaim(taken=True), leg, watch)  # type: ignore[arg-type]
        run_task = asyncio.create_task(program.run())
        # Two ticks: one for run() to start and create the leg's task, one
        # for that inner task to actually begin executing.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert leg.serve_started is True
        watch.end()
        await run_task
        assert leg.cancelled is True

    async def test_a_genuine_leg_crash_is_logged_not_discarded(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.ERROR, logger="punt_vox.panel.program")
        watch = _FakeWatch()
        program = VoxPanelProgram(_FakeClaim(taken=True), _CrashingLeg(), watch)  # type: ignore[arg-type]
        run_task = asyncio.create_task(program.run())
        # Two ticks: one for run() to start and create the leg's task, one
        # for that task to actually run to completion (it raises immediately,
        # with no await of its own).
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        watch.end()
        await run_task

        assert any("crashed" in r.getMessage() for r in caplog.records)
        assert all(r.levelno == logging.ERROR for r in caplog.records)

    async def test_the_expected_cancellation_is_not_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.ERROR, logger="punt_vox.panel.program")
        leg = _FakeLeg()
        watch = _FakeWatch()
        program = VoxPanelProgram(_FakeClaim(taken=True), leg, watch)  # type: ignore[arg-type]
        run_task = asyncio.create_task(program.run())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        watch.end()
        await run_task

        assert caplog.records == []
