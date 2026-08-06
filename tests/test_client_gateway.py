"""Tests for the gateway layer: control DTOs and the client-backed adapter.

The DTOs (:mod:`punt_vox.program_control`) are pure value objects; the adapter
(:class:`ClientProgramGateway`) is a thin verb-adapter that delegates each call
to the matching ``program_*`` method on a ``VoxClientSync`` and returns the
typed value the client already parsed. Tested against a mocked client so no
daemon is needed. Wire parsing itself lives on the client (``test_client.py``).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from punt_vox.client_gateway import ClientProgramGateway
from punt_vox.types_programs import Format, Mode
from punt_vox.types_programs.control import (
    CommandOutcome,
    ProgramSummary,
    SelectionRequest,
    StartRequest,
)
from punt_vox.types_programs.identifiers import ProgramName
from punt_vox.voxd.programs import Part, Program, ProgramState
from punt_vox.voxd.programs.playback_policy import Advance, AdvanceResult


class _AvoidRepeat:
    """Anti-repeat policy stand-in for building a playing Program in tests."""

    def next_part(self, pool: tuple[Part, ...], playing: Part | None) -> AdvanceResult:
        for part in pool:
            if part != playing:
                return Advance(part)
        return Advance(pool[0])


def _build_rotating() -> Program:
    """Drive a fresh Program to playing_rotating with a full 12-Part pool."""
    program = Program(ProgramState.initial(), _AvoidRepeat())
    program.turn_on()
    program.first_track_ok(Part("id001", 1))
    for index in range(2, Format.PLAYLIST.pool_size + 1):
        program.fill_ok(Part(f"id{index:03d}", index))
    return program


# ---------------------------------------------------------------------------
# Control DTOs
# ---------------------------------------------------------------------------


def test_command_outcome_applied_and_rejected() -> None:
    """ok/rejected build the two F7 results with the right applied flag."""
    assert CommandOutcome.ok("on") == CommandOutcome(applied=True, message="on")
    assert CommandOutcome.rejected("lost race").applied is False


def test_start_request_defaults_to_fallback() -> None:
    """An empty StartRequest carries no authoring -- the pool will fall back."""
    request = StartRequest()

    assert request.style is None
    assert request.name is None
    assert request.prompts is None


def test_program_summary_display_line() -> None:
    """A summary renders the handle, id, ready/total counts, and format label."""
    summary = ProgramSummary(
        id="a3f1c9", style="trance", vibe="calm", format="music", ready=5, total=12
    )

    line = summary.display_line()

    assert "trance--calm" in line
    assert "a3f1c9" in line
    assert "5/12" in line
    assert "music" in line


# ---------------------------------------------------------------------------
# ClientProgramGateway -- wire translation
# ---------------------------------------------------------------------------


def test_status_delegates_and_returns_the_client_status() -> None:
    """status() returns the typed ProgramStatus the client already parsed."""
    program = _build_rotating()
    parsed = program.to_status(ProgramName("ambient_techno"))
    client = MagicMock()
    client.program_status.return_value = parsed

    status = ClientProgramGateway(client).status()

    client.program_status.assert_called_once_with()
    assert status is parsed
    assert status.mode is Mode.PLAYING_ROTATING


def test_start_forwards_request_and_returns_the_outcome() -> None:
    """start() forwards the authored request and returns the client's outcome."""
    client = MagicMock()
    client.program_on.return_value = CommandOutcome.ok("on")

    outcome = ClientProgramGateway(client).start(StartRequest(style="techno"))

    client.program_on.assert_called_once_with(
        style="techno", vibe=None, name=None, prompts=None
    )
    assert outcome == CommandOutcome(applied=True, message="on")


def test_advance_returns_the_client_outcome() -> None:
    """advance() delegates to program_next and returns its CommandOutcome."""
    client = MagicMock()
    client.program_next.return_value = CommandOutcome.rejected("lost race")

    outcome = ClientProgramGateway(client).advance()

    client.program_next.assert_called_once_with()
    assert outcome.applied is False
    assert outcome.message == "lost race"


def test_stop_delegates_to_program_stop() -> None:
    """stop() delegates to program_stop and returns its outcome."""
    client = MagicMock()
    client.program_stop.return_value = CommandOutcome.ok("")

    outcome = ClientProgramGateway(client).stop()

    client.program_stop.assert_called_once_with()
    assert outcome.applied is True


def test_command_outcome_display_prefers_reason_over_default() -> None:
    """display() shows the daemon reason when present, the default when silent."""
    assert CommandOutcome.rejected("no track").display("Playing.") == "no track"
    assert CommandOutcome.ok("").display("Playing.") == "Playing."
    assert CommandOutcome.ok("live line").display("Playing.") == "live line"


def test_select_by_tags_forwards_the_query() -> None:
    """select() forwards the tag query (style/vibe/name) to the wire."""
    client = MagicMock()
    client.program_select.return_value = CommandOutcome.ok("ok")

    ClientProgramGateway(client).select(SelectionRequest(style="trance", vibe="calm"))

    client.program_select.assert_called_once_with(
        style="trance", vibe="calm", name=None, album_id=None
    )


def test_select_by_id_forwards_the_album_id() -> None:
    """select() forwards an exact album id as a direct lookup."""
    client = MagicMock()
    client.program_select.return_value = CommandOutcome.ok("ok")

    ClientProgramGateway(client).select(SelectionRequest(id="a3f1c9"))

    client.program_select.assert_called_once_with(
        style=None, vibe=None, name=None, album_id="a3f1c9"
    )


def test_catalog_returns_the_client_summaries() -> None:
    """catalog() delegates to program_list and returns its typed summaries."""
    summaries = (
        ProgramSummary(
            id="a3f1c9",
            style="trance",
            vibe="calm",
            format="music",
            ready=5,
            total=12,
            name="mix",
        ),
        ProgramSummary(
            id="7b2e04", style="lofi", vibe="focus", format="music", ready=1
        ),
    )
    client = MagicMock()
    client.program_list.return_value = summaries

    catalog = ClientProgramGateway(client).catalog()

    client.program_list.assert_called_once_with()
    assert catalog == summaries
