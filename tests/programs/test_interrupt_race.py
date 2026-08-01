"""Tests for :class:`InterruptRace` -- the "how did the part stop?" decision.

The race settles a load's ended-future against a control interrupt and returns a
:class:`TrackEnd`. It covers the outcomes the loop acts on: a user interrupt wins
outright (do not advance); a clean ``eof`` is a natural end (advance); an
``error`` is a bad file (recorded, advance past); and the synthetic ``crashed``
means mpv died (replay the current part). The ended-future always resolves with a
result -- a crash resolves it with ``crashed`` -- so the race never retrieves a
raised awaitable.
"""

from __future__ import annotations

import asyncio

from punt_vox.types_programs.mpv_event import EndFileReason
from punt_vox.voxd.programs.interrupt_race import InterruptRace

from ._mpv_fakes import FakePlayHandle


def _handle() -> FakePlayHandle:
    """Return a handle whose ended-future a test resolves."""
    return FakePlayHandle(asyncio.get_running_loop().create_future())


class TestSettle:
    """The outcomes ``settle`` decides between (reason-based, never an exit code)."""

    async def test_user_interrupt_wins_over_a_running_load(self) -> None:
        interrupt = asyncio.Event()
        interrupt.set()  # a skip / off / play-a-part already posted
        race = InterruptRace(interrupt)
        handle = _handle()  # never resolved -- ended() would block

        end = await race.settle(handle)

        assert end.interrupted is True
        assert end.reason is None
        assert end.faulted is False
        assert end.crashed is False

    async def test_clean_eof_is_a_natural_end(self) -> None:
        race = InterruptRace(asyncio.Event())
        handle = _handle()
        handle.finish(EndFileReason.EOF)  # a natural end, no interrupt pending

        end = await race.settle(handle)

        assert end.interrupted is False
        assert end.reason is EndFileReason.EOF
        assert end.faulted is False
        assert end.crashed is False

    async def test_error_reason_is_a_fault(self) -> None:
        race = InterruptRace(asyncio.Event())
        handle = _handle()
        handle.finish(EndFileReason.ERROR)  # mpv could not play the file

        end = await race.settle(handle)

        assert end.interrupted is False
        assert end.faulted is True  # surfaced, not swallowed as a clean advance
        assert end.crashed is False

    async def test_crashed_reason_is_a_crash(self) -> None:
        race = InterruptRace(asyncio.Event())
        handle = _handle()
        handle.crash()  # the process died; the reader injected ``crashed``

        end = await race.settle(handle)

        assert end.interrupted is False
        assert end.crashed is True  # the loop will replay the current part (I6)
        assert end.faulted is False
