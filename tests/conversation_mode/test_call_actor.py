"""Tests for :class:`CallActor`."""

from __future__ import annotations

from punt_vox.voxd.conversation_mode.barge_in import BargeIn
from punt_vox.voxd.conversation_mode.call_actor import CallActor
from punt_vox.voxd.conversation_mode.capture_during_wait import CaptureDuringWait
from punt_vox.voxd.conversation_mode.end_call import EndCall
from punt_vox.voxd.conversation_mode.mode import Mode
from punt_vox.voxd.conversation_mode.reply_begins import ReplyBegins
from punt_vox.voxd.conversation_mode.reply_ends import ReplyEnds
from punt_vox.voxd.conversation_mode.start_call import StartCall
from punt_vox.voxd.conversation_mode.timeout_call import TimeoutCall
from punt_vox.voxd.conversation_mode.turn_detected import TurnDetected


def test_apply_applies_commands_in_order() -> None:
    actor = CallActor()

    actor.apply(StartCall())
    actor.apply(TurnDetected())
    actor.apply(ReplyBegins())
    actor.apply(ReplyEnds())

    assert actor.mode is Mode.LISTENING


def test_observers_see_each_transition_in_order() -> None:
    actor = CallActor()
    seen: list[tuple[Mode, Mode]] = []
    actor.on_transition(lambda before, after: seen.append((before, after)))

    actor.apply(StartCall())
    actor.apply(EndCall())

    assert seen == [(Mode.IDLE, Mode.LISTENING), (Mode.LISTENING, Mode.IDLE)]


def test_current_detector_reflects_the_actor_s_mode() -> None:
    actor = CallActor()

    actor.apply(StartCall())

    assert actor.current_detector == Mode.LISTENING.active_detector


class TestCommandApplyProtocolConformance:
    """Item 8: exercise ``BargeIn``, ``CaptureDuringWait``, and ``TimeoutCall``
    through :meth:`CallActor.apply` against a real :class:`CallState` --
    every other :class:`~.call_command.CallCommand` implementation
    (``StartCall``, ``EndCall``, ``TurnDetected``, ``ReplyBegins``,
    ``ReplyEnds``) is already exercised this way above; these three were
    previously only reached indirectly, through ``CallState``'s own method
    tests.
    """

    def test_barge_in_applied_directly_moves_speaking_to_listening(self) -> None:
        actor = CallActor()
        actor.apply(StartCall())
        actor.apply(TurnDetected())
        actor.apply(ReplyBegins())
        mode_before = actor.mode
        assert mode_before is Mode.SPEAKING

        actor.apply(BargeIn())

        mode_after = actor.mode
        assert mode_after is Mode.LISTENING
        assert actor.has_pending_addendum is False

    def test_capture_during_wait_applied_directly_stays_in_waiting(self) -> None:
        actor = CallActor()
        actor.apply(StartCall())
        actor.apply(TurnDetected())
        assert actor.mode is Mode.WAITING

        actor.apply(CaptureDuringWait())

        assert actor.mode is Mode.WAITING
        assert actor.has_pending_addendum is True

    def test_timeout_call_applied_directly_moves_listening_to_idle(self) -> None:
        actor = CallActor()
        actor.apply(StartCall())
        mode_before = actor.mode
        assert mode_before is Mode.LISTENING

        actor.apply(TimeoutCall())

        mode_after = actor.mode
        assert mode_after is Mode.IDLE
