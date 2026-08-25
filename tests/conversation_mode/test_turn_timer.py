"""Tests for :class:`LoggingTurnTimer`.

Exercises the real logger via ``caplog`` at DEBUG -- this class always calls
``logger.debug()`` unconditionally; whether that record reaches ``vox.log``,
the terminal, both, or neither is
:func:`punt_vox.logging_config.configure_turn_timer_logging`'s decision (see
``tests/test_logging_config.py``), not this class's.
"""

from __future__ import annotations

import logging

import pytest

from punt_vox.voxd.conversation_mode.turn_timer import LoggingTurnTimer


@pytest.fixture(autouse=True)
def _debug_enabled(  # pyright: ignore[reportUnusedFunction]
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="punt_vox.voxd.conversation_mode.turn_timer")


def test_mark_logs_the_stage_name(caplog: pytest.LogCaptureFixture) -> None:
    LoggingTurnTimer().mark("turn_ended")
    assert "turn_ended" in caplog.text


def test_mark_includes_step_and_turn_elapsed(caplog: pytest.LogCaptureFixture) -> None:
    timer = LoggingTurnTimer()
    timer.mark("speech_first_detected")
    timer.mark("turn_ended")
    (record,) = [r for r in caplog.records if "turn_ended" in r.message]
    assert "step" in record.message
    assert "turn" in record.message


def test_mark_includes_detail_when_given(caplog: pytest.LogCaptureFixture) -> None:
    LoggingTurnTimer().mark("stt_response_received", detail="confidence=0.95")
    assert "confidence=0.95" in caplog.text


def test_mark_omits_detail_suffix_when_none(caplog: pytest.LogCaptureFixture) -> None:
    LoggingTurnTimer().mark("turn_ended")
    assert "--" not in caplog.text


def test_speech_first_detected_resets_the_turn_clock(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A second turn's elapsed-since-turn-start must not include the first
    turn's own duration."""
    timer = LoggingTurnTimer()
    timer.mark("speech_first_detected")
    timer.mark("reply_complete")  # ends turn 1, some real elapsed time later
    timer.mark("speech_first_detected")  # turn 2 begins; clock resets
    reset_records = [
        r for r in caplog.records if r.message.startswith("speech_first_detected")
    ]
    assert len(reset_records) == 2
    # The second speech_first_detected mark is itself the clock reset, so its
    # own reported elapsed time is ~0, not turn 1's accumulated duration.
    assert "+0.0" in reset_records[1].message or "+0.00" in reset_records[1].message
