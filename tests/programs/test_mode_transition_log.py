"""Tests for ModeTransitionLog: a radio has no mode, so None sides are suppressed.

The positive transition (a real Program mode change is logged once) is exercised
end-to-end through the control channel in ``test_control_channel.py``; here the
focus is the subtle suppression rule -- a switch to or from a radio (which has no
lifecycle mode) must never log a ``music: None -> ...`` phantom transition.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from punt_vox.voxd.programs.mode_transition_log import ModeTransitionLog
from punt_vox.voxd.programs.program import Program
from punt_vox.voxd.programs.rotate_policy import RotatePolicy
from punt_vox.voxd.programs.selection import Selection
from punt_vox.voxd.programs.selection_playback import SelectionPlayback
from punt_vox.voxd.programs.state import ProgramState

if TYPE_CHECKING:
    import pytest

_LOGGER = "punt_vox.voxd.programs.mode_transition_log"


def _radio() -> SelectionPlayback:
    """Return an empty replay radio -- a source with no lifecycle mode."""
    return SelectionPlayback(Selection.from_albums(()), RotatePolicy())


def _program() -> Program:
    """Return a fresh idle Program (mode ``off``)."""
    return Program(ProgramState.initial(), RotatePolicy())


def _transitions(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO and "music:" in r.getMessage()
    ]


def test_radio_to_program_logs_no_phantom_none_transition(
    caplog: pytest.LogCaptureFixture,
) -> None:
    log = ModeTransitionLog(_radio())  # prior source has no mode
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        log.note(_program())
    assert _transitions(caplog) == []


def test_program_to_radio_logs_no_phantom_none_transition(
    caplog: pytest.LogCaptureFixture,
) -> None:
    log = ModeTransitionLog(_program())
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        log.note(_radio())  # current source has no mode
    assert _transitions(caplog) == []


def test_radio_to_radio_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    log = ModeTransitionLog(_radio())
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        log.note(_radio())
    assert _transitions(caplog) == []


def test_same_program_mode_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    log = ModeTransitionLog(_program())
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        log.note(_program())  # both off -> no transition
    assert _transitions(caplog) == []
