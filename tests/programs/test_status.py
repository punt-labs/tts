"""Tests for the format-spanning ``ProgramStatus`` observability surface.

Covers the three "not playing / playing" situations a client distinguishes
(idle vs off-with-saved-pool vs playing), that both failure surfaces (the
program-level error and the per-Part failures) reach a client distinctly, and
that the value round-trips through the wire so a client reads the daemon's
authoritative state rather than a log.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from punt_vox.types_programs import Format, Mode, ProgramName, ProgramStatus, Reason
from punt_vox.types_programs.playback_fault import PlaybackFault, PlaybackFaultKind
from punt_vox.types_programs.wire import JsonObject
from punt_vox.voxd.programs import Part, Program, ProgramState

from .conftest import AvoidRepeatPolicy, build_rotating, make_manifest


def _restored(*indices: int) -> Program:
    """Return an idle Program over a saved pool (mode off, nothing playing)."""
    manifest = make_manifest(*indices)
    state = ProgramState.restored(Format.PLAYLIST, frozenset(manifest.ready_parts()))
    return Program(state, AvoidRepeatPolicy())


def test_idle_has_no_name() -> None:
    """An idle daemon reports name=None and off, distinct from a saved pool."""
    status = ProgramStatus.idle()

    assert status.name is None
    assert status.is_idle
    assert status.mode is Mode.OFF
    assert status.now_playing is None
    assert status.failed_parts == ()


def test_off_with_saved_pool_names_the_program() -> None:
    """An off Program with a disk pool reports name set, off, nothing playing (O1)."""
    status = _restored(1, 2, 3).to_status(ProgramName("x"))

    assert status.name == ProgramName("x")
    assert not status.is_idle
    assert status.mode is Mode.OFF
    assert status.now_playing is None  # a pool to play, but not playing


def test_is_playing_is_the_audible_gate() -> None:
    """is_playing holds only in a playing mode -- idle and off are not audible."""
    assert not ProgramStatus.idle().is_playing
    assert not _restored(1, 2).to_status(ProgramName("saved")).is_playing
    assert build_rotating(AvoidRepeatPolicy()).to_status(ProgramName("r")).is_playing


def test_playing_reports_part_n_of_m() -> None:
    """A rotating Program reports the playing Part's 1-based position and pool size."""
    program = build_rotating(AvoidRepeatPolicy())

    status = program.to_status(ProgramName("ambient_techno"))

    assert status.mode is Mode.PLAYING_ROTATING
    assert status.now_playing is not None
    assert status.now_playing.of == Format.PLAYLIST.pool_size
    assert 1 <= status.now_playing.index <= status.now_playing.of


def test_now_playing_reports_position_not_intrinsic_index_across_a_gap() -> None:
    """A gapped pool reports position-of-count, so ``index`` never exceeds ``of``.

    Ready indices 1, 2, 4 (index 3 failed) with the index-4 Part playing must
    report "part 3 of 3" -- its 1-based position in the ordered pool -- never the
    intrinsic-index "4 of 3", which would read as nonsense.
    """
    pool = frozenset({Part("id001", 1), Part("id002", 2), Part("id004", 4)})
    program = Program(ProgramState.restored(Format.PLAYLIST, pool), AvoidRepeatPolicy())
    program.start_from_disk(Part("id004", 4))

    status = program.to_status(ProgramName("gapped"))

    assert status.now_playing is not None
    assert status.now_playing.index == 3  # the position-3 it holds, not intrinsic 4
    assert status.now_playing.of == 3
    assert status.now_playing.index <= status.now_playing.of


def test_both_failure_surfaces_present_and_distinct() -> None:
    """A per-Part failure surfaces in failed_parts while the program plays on."""
    program = Program(ProgramState.initial(), AvoidRepeatPolicy())
    program.turn_on()
    program.first_track_ok(Part("id001", 1))
    program.fill_bad_part(Part("id002", 2), Reason("bad_prompt: unsafe"))

    status = program.to_status(ProgramName("ambient_techno"))

    # Per-Part surface carries the failure; program-level error stays clear.
    assert len(status.failed_parts) == 1
    assert status.failed_parts[0].index == 2
    assert "bad_prompt" in status.failed_parts[0].reason
    assert status.generation.last_error is None
    assert status.now_playing is not None  # still playing


def test_program_level_failure_sets_last_error() -> None:
    """A first-track permanent failure surfaces as a program-level error."""
    program = Program(ProgramState.initial(), AvoidRepeatPolicy())
    program.turn_on()
    program.first_track_bad_prompt(Part("id001", 1), Reason("bad_prompt: nope"))

    status = program.to_status(ProgramName("ambient_techno"))

    assert status.mode is Mode.FAILED
    assert status.generation.last_error is not None
    assert "bad_prompt" in status.generation.last_error
    assert status.now_playing is None


@pytest.mark.parametrize(
    "build",
    [
        ProgramStatus.idle,
        lambda: _restored(1, 2).to_status(ProgramName("saved")),
        lambda: build_rotating(AvoidRepeatPolicy()).to_status(ProgramName("r")),
    ],
)
def test_wire_round_trips(build: Callable[[], ProgramStatus]) -> None:
    """Every status shape survives a JSON round-trip unchanged (client-observable)."""
    original = build()

    restored = ProgramStatus.from_wire(JsonObject.coerce(original.to_dict(), "status"))

    assert restored == original


def test_playback_error_surfaces_and_round_trips() -> None:
    """A player spawn fault reaches a client via status, not a log."""
    program = build_rotating(AvoidRepeatPolicy())
    fault = PlaybackFault(
        part_index=2,
        reason="afplay: No such file or directory",
        kind=PlaybackFaultKind.PLAYER_UNAVAILABLE,
    )

    original = program.to_status(ProgramName("ambient_techno"), fault)

    assert original.playback_error == fault
    restored = ProgramStatus.from_wire(JsonObject.coerce(original.to_dict(), "status"))
    assert restored == original
    assert restored.playback_error is not None
    assert restored.playback_error.part_index == 2
    assert "No such file" in restored.playback_error.reason
    assert restored.playback_error.kind is PlaybackFaultKind.PLAYER_UNAVAILABLE


def test_radio_surfaces_a_playback_fault() -> None:
    """A replay/radio track fault reaches a client via status, not only a log.

    A missing or corrupt saved track exits non-zero; the fault must be as visible
    on a consume-only radio as on a generate Program.
    """
    fault = PlaybackFault(
        part_index=3,
        reason="player exited with code 1",
        kind=PlaybackFaultKind.TRACK_ERROR,
    )

    status = ProgramStatus.radio(ProgramName("radio"), None, fault)

    assert status.playback_error == fault
    restored = ProgramStatus.from_wire(JsonObject.coerce(status.to_dict(), "status"))
    assert restored == status
    assert restored.playback_error is not None
    assert restored.playback_error.part_index == 3
    assert restored.playback_error.kind is PlaybackFaultKind.TRACK_ERROR


def test_healthy_radio_has_no_playback_error() -> None:
    """A radio with no fault carries playback_error None (the healthy contract)."""
    status = ProgramStatus.radio(ProgramName("radio"), None)

    assert status.playback_error is None


def test_healthy_player_has_no_playback_error() -> None:
    """Absent a spawn fault, status carries playback_error None (healthy contract)."""
    program = build_rotating(AvoidRepeatPolicy())

    status = program.to_status(ProgramName("ambient_techno"))

    assert status.playback_error is None
    assert status.to_dict()["playback_error"] is None


def test_wire_round_trips_failed_parts() -> None:
    """A failed-Part surface survives the wire so a client sees it, not a log."""
    program = Program(ProgramState.initial(), AvoidRepeatPolicy())
    program.turn_on()
    program.first_track_ok(Part("id001", 1))
    program.fill_bad_part(Part("id002", 2), Reason("ToS violation"))
    original = program.to_status(ProgramName("ambient_techno"))

    restored = ProgramStatus.from_wire(JsonObject.coerce(original.to_dict(), "status"))

    assert restored == original
    assert restored.failed_parts[0].reason == "ToS violation"
