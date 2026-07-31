"""Tests for :class:`InterruptRace` -- the "how did the track stop?" decision.

These port the unit-level ``_exit_code`` assertions that lived on ``ProgramLoop``
before the race machinery was extracted, and cover the four outcomes ``settle``
decides between: a user interrupt wins outright, a clean player exit (code 0) is a
natural end, a *raised* ``wait`` is a player error retrieved and logged so it never
masquerades as a clean advance, and a non-zero exit is a fault.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd.programs.interrupt_race import InterruptRace

if TYPE_CHECKING:
    import pytest


@final
class _CleanProcess:
    """A process that exits cleanly the moment its end is signalled (test control)."""

    __slots__ = ("_ended", "rc")
    rc: int | None
    _ended: asyncio.Event

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self.rc = None
        self._ended = asyncio.Event()
        return self

    async def wait(self) -> int:
        await self._ended.wait()
        return self.rc if self.rc is not None else 0

    async def kill(self) -> None:
        self.rc = -9
        self._ended.set()

    def suspend(self) -> None:
        """Satisfy the PlayerProcess protocol; the race tests never pause."""

    def resume(self) -> None:
        """Satisfy the PlayerProcess protocol; the race tests never pause."""

    def end(self, rc: int = 0) -> None:
        self.rc = rc
        self._ended.set()


@final
class _RaisingProcess:
    """A process whose ``wait`` raises -- a player error, not a clean end."""

    __slots__ = ()

    async def wait(self) -> int:
        msg = "transport gone"
        raise RuntimeError(msg)

    async def kill(self) -> None:
        return None

    def suspend(self) -> None:
        """Satisfy the PlayerProcess protocol; the race tests never pause."""

    def resume(self) -> None:
        """Satisfy the PlayerProcess protocol; the race tests never pause."""


class TestExitCode:
    """A settled ``wait`` that raised yields ``None`` (error); a clean one, its code."""

    async def test_flags_a_raised_wait(self, caplog: pytest.LogCaptureFixture) -> None:
        async def _boom() -> int:
            msg = "transport gone"
            raise RuntimeError(msg)

        task: asyncio.Task[int] = asyncio.ensure_future(_boom())
        with contextlib.suppress(RuntimeError):
            await task
        with caplog.at_level(logging.ERROR):
            code = InterruptRace._exit_code(task)
        assert code is None
        assert any("player wait failed" in r.getMessage() for r in caplog.records)

    async def test_returns_a_clean_exit_code(self) -> None:
        async def _clean() -> int:
            return 0

        task: asyncio.Task[int] = asyncio.ensure_future(_clean())
        await task
        assert InterruptRace._exit_code(task) == 0


class TestSettle:
    """The four outcomes ``settle`` decides between (F3 adds the non-zero exit)."""

    async def test_user_interrupt_wins_over_a_running_player(self) -> None:
        interrupt = asyncio.Event()
        interrupt.set()  # a skip / off / play-a-part already posted
        race = InterruptRace(interrupt)
        proc = _CleanProcess()  # still playing -- wait() would block

        end = await race.settle(proc)
        assert end.interrupted is True
        assert end.faulted is False

    async def test_clean_end_is_neither_interrupted_nor_faulted(self) -> None:
        interrupt = asyncio.Event()
        race = InterruptRace(interrupt)
        proc = _CleanProcess()
        proc.end(0)  # a natural end, no interrupt pending

        end = await race.settle(proc)
        assert end.interrupted is False
        assert end.faulted is False
        assert end.exit_code == 0

    async def test_non_zero_exit_is_a_fault(self) -> None:
        interrupt = asyncio.Event()
        race = InterruptRace(interrupt)
        proc = _CleanProcess()
        proc.end(1)  # a missing/corrupt track -- the player exits non-zero

        end = await race.settle(proc)
        assert end.interrupted is False
        assert end.faulted is True  # surfaced, not swallowed as a clean advance
        assert end.exit_code == 1

    async def test_raised_wait_is_interrupted_and_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        interrupt = asyncio.Event()
        race = InterruptRace(interrupt)
        with caplog.at_level(logging.ERROR):
            end = await race.settle(_RaisingProcess())
        assert end.interrupted is True  # a player error is not a clean advance
        assert end.faulted is False
        assert any("player wait failed" in r.getMessage() for r in caplog.records)
