"""Tests for :class:`PlaybackHealth` and :class:`PlaybackFault`.

The health slot records a player fault so ``status`` can surface it, and clears it
on recovery. The fault value round-trips through the wire so a client reads a
standing playback problem, never only a daemon log. Two families of kind share
the surface: a per-part ``TRACK_ERROR`` (mpv could not play a file) and the
process-level mpv faults (``PLAYER_UNAVAILABLE``/``PLAYER_CRASH``/``PLAYER_FAILED``).
"""

from __future__ import annotations

from punt_vox.types_programs.wire import JsonObject
from punt_vox.voxd.programs.part import Part
from punt_vox.voxd.programs.playback_health import (
    PlaybackFault,
    PlaybackFaultKind,
    PlaybackHealth,
)


def test_starts_healthy() -> None:
    """A fresh health slot reports no fault."""
    assert PlaybackHealth().fault is None


def test_records_a_track_error() -> None:
    """Recording a per-part track error exposes the Part, reason, and kind."""
    health = PlaybackHealth()

    health.record(
        Part("003.mp3", 3),
        "mpv could not play the part file",
        PlaybackFaultKind.TRACK_ERROR,
    )

    fault = health.fault
    assert fault is not None
    assert fault.part_index == 3
    assert "could not play" in fault.reason
    assert fault.kind is PlaybackFaultKind.TRACK_ERROR


def test_records_a_load_unavailable_fault_distinctly() -> None:
    """A load mpv would not accept is recorded with the player-unavailable kind."""
    health = PlaybackHealth()

    health.record(
        Part("004.mp3", 4),
        "mpv did not accept the load",
        PlaybackFaultKind.PLAYER_UNAVAILABLE,
    )

    fault = health.fault
    assert fault is not None
    assert fault.kind is PlaybackFaultKind.PLAYER_UNAVAILABLE


def test_clear_restores_health() -> None:
    """A successful load clears the standing fault."""
    health = PlaybackHealth()
    health.record(Part("001.mp3", 1), "boom", PlaybackFaultKind.TRACK_ERROR)

    health.clear()

    assert health.fault is None


def test_latest_failure_replaces_the_prior() -> None:
    """Only the standing fault is kept -- a new failure supersedes the old."""
    health = PlaybackHealth()
    health.record(Part("001.mp3", 1), "first", PlaybackFaultKind.TRACK_ERROR)
    health.record(Part("002.mp3", 2), "second", PlaybackFaultKind.PLAYER_UNAVAILABLE)

    fault = health.fault
    assert fault is not None
    assert fault.part_index == 2
    assert fault.reason == "second"
    assert fault.kind is PlaybackFaultKind.PLAYER_UNAVAILABLE


def test_process_level_fault_carries_no_part() -> None:
    """A process-level mpv fault names no single part (index 0)."""
    fault = PlaybackFault.process_level(
        "mpv crashed; restarting", PlaybackFaultKind.PLAYER_CRASH
    )
    assert fault.part_index == 0
    assert fault.kind is PlaybackFaultKind.PLAYER_CRASH


def test_fault_round_trips_through_wire() -> None:
    """The fault survives a JSON round-trip so a client reads it, not a log."""
    original = PlaybackFault(
        part_index=4,
        reason="mpv could not be kept running",
        kind=PlaybackFaultKind.PLAYER_FAILED,
    )

    restored = PlaybackFault.from_wire(JsonObject.coerce(original.to_dict(), "fault"))

    assert restored == original
    assert restored.kind is PlaybackFaultKind.PLAYER_FAILED
