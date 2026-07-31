"""Tests for ``PlaybackSuspension`` -- the click-free pause/resume seam.

Pause no longer freezes the player in place. It tears the player down gracefully
(so the audio device closes with no underrun click) and records *where* the part
was; resume re-spawns the player seeked to that offset. ``T3`` -- a paused album
never auto-advances -- holds because a paused source has no running player at all,
so the loop parks on the gate and spawns nothing. A ``prev``/``next`` while paused
moves the cursor alone, and the loop plays the newly-cursored part on resume at
offset~0 (Fork~B).
"""

from __future__ import annotations

import asyncio
from typing import final

from punt_vox.voxd.programs import Part
from punt_vox.voxd.programs.suspension import PlaybackSuspension


@final
class _FakeProcess:
    """A PlayerProcess double recording the graceful-stop and terminate calls."""

    def __init__(self) -> None:
        self.stops = 0
        self.terminates = 0
        self.kills = 0

    async def wait(self) -> int:  # pragma: no cover - unused here
        raise NotImplementedError

    async def kill(self) -> None:
        self.kills += 1

    def stop_gracefully(self) -> None:
        self.stops += 1

    def terminate(self) -> None:
        self.terminates += 1


@final
class _Clock:
    """A monotonic clock a test drives by hand, to pin the elapsed-offset math."""

    def __init__(self) -> None:
        self._now = 0.0

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def __call__(self) -> float:
        return self._now


_PART = Part("id001", 1)
_OTHER = Part("id002", 2)


def test_new_suspension_is_not_paused() -> None:
    suspension = PlaybackSuspension()
    assert suspension.is_paused is False


def test_pause_stops_the_attached_handle_gracefully() -> None:
    suspension = PlaybackSuspension()
    proc = _FakeProcess()
    suspension.attach(proc, _PART, 0.0)

    suspension.pause()

    assert suspension.is_paused is True
    assert proc.stops == 1  # the live player was torn down with SIGTERM, not frozen


def test_resume_opens_the_gate_without_touching_a_handle() -> None:
    # The paused player is already gone; resume only opens the gate so the loop
    # re-spawns. There is no live handle to continue.
    suspension = PlaybackSuspension()
    proc = _FakeProcess()
    suspension.attach(proc, _PART, 0.0)
    suspension.pause()

    suspension.resume()

    assert suspension.is_paused is False


def test_pause_is_idempotent() -> None:
    suspension = PlaybackSuspension()
    proc = _FakeProcess()
    suspension.attach(proc, _PART, 0.0)
    suspension.pause()
    suspension.pause()  # already paused -> no second SIGTERM
    assert proc.stops == 1


def test_resume_is_idempotent() -> None:
    suspension = PlaybackSuspension()
    suspension.resume()  # not paused -> a no-op
    assert suspension.is_paused is False


def test_attach_while_paused_stops_the_new_handle() -> None:
    # The spawn-during-pause race: pause landed before this spawn, so the freshly
    # attached handle must be stopped on arrival -- it must never play.
    suspension = PlaybackSuspension()
    suspension.pause()

    proc = _FakeProcess()
    suspension.attach(proc, _PART, 0.0)

    assert proc.stops == 1


def test_reset_clears_paused_and_resume_state() -> None:
    # A stop/switch resets: the suspension returns to the open, not-paused state
    # and drops any frozen resume point (the next source starts fresh).
    suspension = PlaybackSuspension()
    proc = _FakeProcess()
    suspension.attach(proc, _PART, 5.0)
    suspension.pause()

    suspension.reset()

    assert suspension.is_paused is False
    assert suspension.seek_for(_PART) == 0.0  # the resume point was dropped


def test_seek_for_returns_the_frozen_offset_for_the_paused_part() -> None:
    # Pause freezes the elapsed offset; resuming the SAME part seeks back to it.
    clock = _Clock()
    suspension = PlaybackSuspension(clock)
    proc = _FakeProcess()
    suspension.attach(proc, _PART, 0.0)
    clock.advance(30.0)  # 30s played

    suspension.pause()

    assert suspension.seek_for(_PART) == 30.0


def test_seek_for_a_different_part_is_zero() -> None:
    # A prev/next moved the cursor while paused: the new part plays from its
    # start, not the frozen offset of the part that was paused (Fork B).
    clock = _Clock()
    suspension = PlaybackSuspension(clock)
    proc = _FakeProcess()
    suspension.attach(proc, _PART, 0.0)
    clock.advance(30.0)
    suspension.pause()

    assert suspension.seek_for(_OTHER) == 0.0


def test_offset_accumulates_across_pause_resume_cycles() -> None:
    # Pause at 10s, resume (re-spawn seeked to 10s), play 5s more, pause again:
    # the frozen offset is the total 15s, not just the last 5s.
    clock = _Clock()
    suspension = PlaybackSuspension(clock)

    first = _FakeProcess()
    suspension.attach(first, _PART, 0.0)
    clock.advance(10.0)
    suspension.pause()
    assert suspension.seek_for(_PART) == 10.0

    suspension.resume()
    suspension.detach()  # the loop settled the stopped first handle
    second = _FakeProcess()
    suspension.attach(second, _PART, 10.0)  # re-spawned seeked to 10s
    clock.advance(5.0)
    suspension.pause()

    assert suspension.seek_for(_PART) == 15.0


def test_pause_with_no_live_handle_records_no_offset() -> None:
    # Pausing while nothing plays (e.g. generating_first): there is no live track,
    # so no resume point is frozen and the eventual first part plays from 0.
    suspension = PlaybackSuspension()
    suspension.pause()
    assert suspension.is_paused is True
    assert suspension.seek_for(_PART) == 0.0


def test_detach_after_natural_end_drops_the_resume_point() -> None:
    # A natural track end (not paused) advances to a new part, so the resume point
    # is cleared -- the next part starts fresh.
    clock = _Clock()
    suspension = PlaybackSuspension(clock)
    proc = _FakeProcess()
    suspension.attach(proc, _PART, 7.0)

    suspension.detach()

    assert suspension.seek_for(_PART) == 0.0


def test_detach_while_paused_keeps_the_resume_point() -> None:
    # The pause path detaches the settled (gracefully-stopped) handle but must
    # keep the frozen offset for the resume spawn.
    clock = _Clock()
    suspension = PlaybackSuspension(clock)
    proc = _FakeProcess()
    suspension.attach(proc, _PART, 0.0)
    clock.advance(12.0)
    suspension.pause()

    suspension.detach()

    assert suspension.seek_for(_PART) == 12.0


async def test_gate_blocks_while_paused_and_opens_on_resume() -> None:
    # The loop gate: wait_resumed blocks while paused (so nothing plays until
    # resume) and returns at once when playing.
    suspension = PlaybackSuspension()
    suspension.pause()
    waiter = asyncio.ensure_future(suspension.wait_resumed())
    await asyncio.sleep(0)
    assert not waiter.done()  # parked while paused

    suspension.resume()
    await asyncio.wait_for(waiter, timeout=1.0)  # released on resume
    assert waiter.done()


async def test_gate_is_open_when_not_paused() -> None:
    suspension = PlaybackSuspension()
    await asyncio.wait_for(suspension.wait_resumed(), timeout=1.0)  # returns at once


def test_shutdown_terminates_a_live_player() -> None:
    # A player playing at daemon stop must be killed, not orphaned.
    suspension = PlaybackSuspension()
    proc = _FakeProcess()
    suspension.attach(proc, _PART, 0.0)

    suspension.shutdown()

    assert proc.terminates == 1  # the live player was SIGKILL-ed
    assert suspension.is_paused is False  # and the state is reset


def test_shutdown_with_no_player_is_a_no_op() -> None:
    suspension = PlaybackSuspension()
    suspension.shutdown()  # nothing attached -> nothing to terminate
    assert suspension.is_paused is False
