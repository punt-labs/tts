"""Tests for :class:`CallActor`'s serialized dispatch loop."""

from __future__ import annotations

import asyncio

from punt_vox.voxd.conversation_mode.call_actor import CallActor
from punt_vox.voxd.conversation_mode.end_call import EndCall
from punt_vox.voxd.conversation_mode.mode import Mode
from punt_vox.voxd.conversation_mode.reply_begins import ReplyBegins
from punt_vox.voxd.conversation_mode.reply_ends import ReplyEnds
from punt_vox.voxd.conversation_mode.start_call import StartCall
from punt_vox.voxd.conversation_mode.turn import TranscribedTurn
from punt_vox.voxd.conversation_mode.turn_detected import TurnDetected


async def test_run_applies_commands_in_order_then_stops() -> None:
    actor = CallActor()
    runner = asyncio.create_task(actor.run())

    await actor.enqueue(StartCall())
    await actor.enqueue(TurnDetected(turn=TranscribedTurn(text="hello")))
    await actor.enqueue(ReplyBegins())
    await actor.enqueue(ReplyEnds())
    await actor.stop()
    await runner

    assert actor.mode is Mode.LISTENING


async def test_observers_see_each_transition_in_order() -> None:
    actor = CallActor()
    seen: list[tuple[Mode, Mode]] = []
    actor.on_transition(lambda before, after: seen.append((before, after)))
    runner = asyncio.create_task(actor.run())

    await actor.enqueue(StartCall())
    await actor.enqueue(EndCall())
    await actor.stop()
    await runner

    assert seen == [(Mode.IDLE, Mode.LISTENING), (Mode.LISTENING, Mode.IDLE)]


async def test_current_detector_reflects_the_actor_s_mode() -> None:
    actor = CallActor()
    runner = asyncio.create_task(actor.run())

    await actor.enqueue(StartCall())
    await actor.stop()
    await runner

    assert actor.current_detector == Mode.LISTENING.active_detector
