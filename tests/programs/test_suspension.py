"""Tests for ``PlaybackSuspension`` -- the pause/resume seam over the live player.

The suspension is the mechanism behind the transport's ``pause``/``resume``: it
suspends the running player in place (so ``T3`` -- a paused album never
auto-advances -- holds because the ``SIGSTOP``-ed player never exits) and gates the
playback loop so a paused player stays paused across a prev/next reposition (Fork B).
"""

from __future__ import annotations

import asyncio
from typing import final

from punt_vox.voxd.programs.suspension import PlaybackSuspension


@final
class _FakeProcess:
    """A PlayerProcess double recording suspend/resume calls (never truly stops)."""

    def __init__(self) -> None:
        self.suspends = 0
        self.resumes = 0

    async def wait(self) -> int:  # pragma: no cover - unused here
        raise NotImplementedError

    async def kill(self) -> None:  # pragma: no cover - unused here
        raise NotImplementedError

    def suspend(self) -> None:
        self.suspends += 1

    def resume(self) -> None:
        self.resumes += 1


def test_new_suspension_is_not_paused_and_open() -> None:
    suspension = PlaybackSuspension()
    assert suspension.is_paused is False


def test_pause_suspends_the_attached_handle() -> None:
    suspension = PlaybackSuspension()
    proc = _FakeProcess()
    suspension.attach(proc)

    suspension.pause()

    assert suspension.is_paused is True
    assert proc.suspends == 1  # the live player was SIGSTOP-ed


def test_resume_continues_the_attached_handle() -> None:
    suspension = PlaybackSuspension()
    proc = _FakeProcess()
    suspension.attach(proc)
    suspension.pause()

    suspension.resume()

    assert suspension.is_paused is False
    assert proc.resumes == 1  # SIGCONT-ed from where it stopped


def test_pause_is_idempotent() -> None:
    suspension = PlaybackSuspension()
    proc = _FakeProcess()
    suspension.attach(proc)
    suspension.pause()
    suspension.pause()  # already paused -> no second SIGSTOP
    assert proc.suspends == 1


def test_resume_is_idempotent() -> None:
    suspension = PlaybackSuspension()
    proc = _FakeProcess()
    suspension.attach(proc)
    suspension.resume()  # not paused -> no SIGCONT
    assert proc.resumes == 0


def test_attach_while_paused_suspends_the_new_handle() -> None:
    # The spawn-during-pause race: pause landed before the next spawn, so the
    # freshly attached handle must be suspended on arrival.
    suspension = PlaybackSuspension()
    suspension.pause()

    proc = _FakeProcess()
    suspension.attach(proc)

    assert proc.suspends == 1


def test_reset_continues_a_paused_handle_and_clears_state() -> None:
    # A stop/switch resets: the held player is continued (never left a stopped
    # orphan) and the suspension returns to the open, not-paused state.
    suspension = PlaybackSuspension()
    proc = _FakeProcess()
    suspension.attach(proc)
    suspension.pause()

    suspension.reset()

    assert suspension.is_paused is False
    assert proc.resumes == 1  # continued so no stopped orphan is left behind


async def test_gate_blocks_while_paused_and_opens_on_resume() -> None:
    # The loop gate: wait_resumed blocks while paused (so a repositioned paused
    # player does not play until resume, Fork B) and returns at once when playing.
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
