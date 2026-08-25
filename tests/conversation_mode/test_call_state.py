"""Unit tests for :class:`CallState`, one per Z operation schema."""

from __future__ import annotations

import pytest

from punt_vox.voxd.conversation_mode.call_state import CallState, IllegalTransitionError
from punt_vox.voxd.conversation_mode.mode import Detector, Mode


def test_starts_idle_with_no_pending_addendum() -> None:
    state = CallState()
    assert state.mode is Mode.IDLE
    assert state.has_pending_addendum is False


def test_start_call_goes_straight_to_listening() -> None:
    state = CallState()
    state.start_call()
    assert state.mode is Mode.LISTENING


def test_start_call_requires_idle() -> None:
    state = CallState()
    state.start_call()
    with pytest.raises(IllegalTransitionError):
        state.start_call()


def test_end_call_returns_to_idle_from_any_active_mode() -> None:
    state = CallState()
    state.start_call()
    state.turn_detected()
    state.reply_begins()  # now speaking
    state.end_call()
    assert state.mode is Mode.IDLE
    assert state.has_pending_addendum is False


def test_end_call_requires_an_active_call() -> None:
    state = CallState()
    with pytest.raises(IllegalTransitionError):
        state.end_call()


def test_timeout_call_only_fires_from_listening() -> None:
    state = CallState()
    state.start_call()
    state.timeout_call()
    assert state.mode is Mode.IDLE

    state.start_call()
    state.turn_detected()
    with pytest.raises(IllegalTransitionError):
        state.timeout_call()


def test_turn_detected_moves_listening_to_waiting() -> None:
    state = CallState()
    state.start_call()
    state.turn_detected()
    assert state.mode is Mode.WAITING


def test_capture_during_wait_sets_pending_addendum() -> None:
    state = CallState()
    state.start_call()
    state.turn_detected()
    state.capture_during_wait()
    assert state.mode is Mode.WAITING
    assert state.has_pending_addendum is True


def test_reply_begins_carries_pending_addendum_through() -> None:
    state = CallState()
    state.start_call()
    state.turn_detected()
    state.capture_during_wait()
    state.reply_begins()
    assert state.mode is Mode.SPEAKING
    assert state.has_pending_addendum is True


def test_barge_in_discharges_pending_addendum_on_return_to_listening() -> None:
    state = CallState()
    state.start_call()
    state.turn_detected()
    state.capture_during_wait()
    state.reply_begins()
    state.barge_in()
    assert state.mode is Mode.LISTENING
    assert state.has_pending_addendum is False


def test_reply_ends_discharges_pending_addendum_on_return_to_listening() -> None:
    state = CallState()
    state.start_call()
    state.turn_detected()
    state.capture_during_wait()
    state.reply_begins()
    state.reply_ends()
    assert state.mode is Mode.LISTENING
    assert state.has_pending_addendum is False


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (Mode.IDLE, Detector.NONE),
        (Mode.LISTENING, Detector.TURN),
        (Mode.WAITING, Detector.TURN),
        (Mode.SPEAKING, Detector.BARGE_IN),
    ],
)
def test_current_detector_matches_active_detector_mapping(
    mode: Mode, expected: Detector
) -> None:
    assert mode.active_detector is expected
