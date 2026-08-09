"""Tests for ``MusicStateView`` -- the music fields every MCP status surface reports.

The point of the view is that ``mic:status`` and ``music`` with
``subcommand="status"`` cannot drift: both derive ``music_mode`` from the same
``program`` block they report, and both describe an unreachable daemon the same
way. These tests pin that derivation and the unreachable projection.
"""

from __future__ import annotations

from punt_vox.music_state_view import MusicStateView
from punt_vox.types_programs import Mode, ProgramName, ProgramStatus
from punt_vox.voxd.programs import Advance, AdvanceResult, Part, Program, ProgramState


class _FirstDifferentPart:
    """Anti-repeat policy stand-in for building a playing Program in tests."""

    def next_part(self, pool: tuple[Part, ...], playing: Part | None) -> AdvanceResult:
        for part in pool:
            if part != playing:
                return Advance(part)
        return Advance(pool[0])


def _playing() -> ProgramStatus:
    program = Program(ProgramState.initial(), _FirstDifferentPart())
    program.turn_on()
    program.first_track_ok(Part("id001", 1))
    return program.to_status(ProgramName("ambient_techno"))


def test_an_idle_daemon_reports_music_off() -> None:
    payload = MusicStateView.of(ProgramStatus.idle()).to_dict()

    assert payload["music_mode"] == "off"
    assert payload["program"] == ProgramStatus.idle().to_dict()


def test_a_playing_program_reports_music_on() -> None:
    status = _playing()

    payload = MusicStateView.of(status).to_dict()

    assert payload["music_mode"] == "on"
    assert payload["program"] == status.to_dict()


def test_the_label_never_contradicts_the_program_block() -> None:
    """The label is derived from the block it ships with, so they cannot disagree."""
    for status in (ProgramStatus.idle(), _playing()):
        payload = MusicStateView.of(status).to_dict()
        program = payload["program"]
        assert isinstance(program, dict)
        off = program["mode"] == Mode.OFF.value
        assert payload["music_mode"] == ("off" if off else "on")


def test_an_unreachable_daemon_reports_the_fault_and_off() -> None:
    """Nothing can be confirmed playing, and the reason reaches the client."""
    payload = MusicStateView.unavailable("voxd unreachable").to_dict()

    assert payload["program"] == {"error": "voxd unreachable"}
    assert payload["music_mode"] == "off"
