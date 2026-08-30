"""Offline tests for the barge-in trace adjudicator.

Each scenario is a hand-built event sequence proving one ruling: the
happy path passes all four criteria, and each criterion fails (or the
verdict goes INCONCLUSIVE) on exactly the evidence it claims to check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from barge_in_verdict import BargeInAdjudicator, TraceEvent, Verdict
from convai import EventTrace

if TYPE_CHECKING:
    from pathlib import Path


def _event(ms: float, direction: str, event_type: str, **body: object) -> TraceEvent:
    # seq mirrors ms: the hand-built scenarios express ordering once.
    return TraceEvent(
        seq=int(ms), ms=ms, direction=direction, event_type=event_type, body=body
    )


def _happy_events() -> list[TraceEvent]:
    return [
        _event(1000, "note", "barge_in_step", step="trigger_search"),
        _event(2000, "recv", "client_tool_call", tool="search_code", tool_call_id="c1"),
        _event(3000, "recv", "interruption", event_id=1),
        _event(3100, "recv", "agent_response_correction"),
        _event(
            4200,
            "send",
            "client_tool_result",
            tool="search_code",
            tool_call_id="c1",
            is_error=False,
        ),
        _event(5200, "recv", "agent_response", text="I found 3 matches."),
        _event(6000, "note", "barge_in_step", step="probe_recall"),
        _event(
            7000,
            "recv",
            "agent_response",
            text="The search found the voxd daemon dispatch and the provider registry.",
        ),
        _event(8000, "note", "barge_in_step", step="note_roundtrip"),
        _event(9000, "recv", "client_tool_call", tool="write_note", tool_call_id="c2"),
        _event(
            9100,
            "send",
            "client_tool_result",
            tool="write_note",
            tool_call_id="c2",
            is_error=False,
        ),
        _event(9500, "recv", "agent_response", text="Note saved."),
        _event(10000, "note", "session_closed"),
    ]


class TestHappyPath:
    """All four criteria hold on a clean barge-in trace."""

    def test_verdict_is_pass(self) -> None:
        verdict = BargeInAdjudicator(_happy_events()).adjudicate()
        assert verdict.verdict is Verdict.PASSED

    def test_all_four_criteria_pass(self) -> None:
        verdict = BargeInAdjudicator(_happy_events()).adjudicate()
        assert [c.passed for c in verdict.criteria] == [True, True, True, True]

    def test_answer_text_is_quoted(self) -> None:
        verdict = BargeInAdjudicator(_happy_events()).adjudicate()
        assert "voxd daemon dispatch" in verdict.answer_text

    def test_summary_carries_the_verdict(self) -> None:
        verdict = BargeInAdjudicator(_happy_events()).adjudicate()
        assert verdict.summary().startswith("barge-in state integrity: PASS")


class TestInconclusive:
    """Scenario-not-reached traces rule INCONCLUSIVE, never FAIL."""

    def test_no_search_call(self) -> None:
        events = [e for e in _happy_events() if e.body.get("tool") != "search_code"]
        verdict = BargeInAdjudicator(events).adjudicate()
        assert verdict.verdict is Verdict.INCONCLUSIVE

    def test_no_interruption_event(self) -> None:
        events = [
            e
            for e in _happy_events()
            if e.event_type not in ("interruption", "agent_response_correction")
        ]
        verdict = BargeInAdjudicator(events).adjudicate()
        assert verdict.verdict is Verdict.INCONCLUSIVE

    def test_no_interruption_evidence_names_vad(self) -> None:
        events = [
            e
            for e in _happy_events()
            if e.event_type not in ("interruption", "agent_response_correction")
        ]
        verdict = BargeInAdjudicator(events).adjudicate()
        assert "VAD" in verdict.criteria[0].evidence


class TestCriterionFailures:
    """Each criterion fails on exactly the evidence it checks."""

    def test_interruption_after_post_tool_response_fails(self) -> None:
        events = [
            e
            for e in _happy_events()
            if e.event_type not in ("interruption", "agent_response_correction")
        ]
        events.append(_event(5900, "recv", "interruption", event_id=9))
        verdict = BargeInAdjudicator(events).adjudicate()
        assert verdict.verdict is Verdict.FAILED
        assert verdict.criteria[0].passed is False

    def test_ws_close_fails_survival(self) -> None:
        events = [*_happy_events(), _event(9600, "note", "ws_closed", reason="1011")]
        verdict = BargeInAdjudicator(events).adjudicate()
        assert verdict.criteria[1].passed is False

    def test_no_agent_turn_after_barge_in_fails_survival(self) -> None:
        events = [
            e
            for e in _happy_events()
            if not (e.event_type == "agent_response" and e.ms > 3000)
        ]
        verdict = BargeInAdjudicator(events).adjudicate()
        assert verdict.criteria[1].passed is False

    def test_question_echo_does_not_pass_recall(self) -> None:
        events = [
            e
            if e.ms != 7000
            else _event(
                7000,
                "recv",
                "agent_response",
                text="You asked about the playback queue.",
            )
            for e in _happy_events()
        ]
        verdict = BargeInAdjudicator(events).adjudicate()
        assert verdict.criteria[2].passed is False

    def test_didnt_find_any_matches_does_not_pass_recall(self) -> None:
        # A bare "match" marker would false-positive on this exact
        # answer; only the count-bearing forms the tool returns count.
        events = [
            e
            if e.ms != 7000
            else _event(
                7000,
                "recv",
                "agent_response",
                text="I didn't find any matches.",
            )
            for e in _happy_events()
        ]
        verdict = BargeInAdjudicator(events).adjudicate()
        assert verdict.criteria[2].passed is False

    def test_negated_answer_naming_a_marker_does_not_pass_recall(self) -> None:
        events = [
            e
            if e.ms != 7000
            else _event(
                7000,
                "recv",
                "agent_response",
                text="I did not find the voxd daemon dispatch you mentioned.",
            )
            for e in _happy_events()
        ]
        verdict = BargeInAdjudicator(events).adjudicate()
        assert verdict.criteria[2].passed is False
        assert "negated" in verdict.criteria[2].evidence

    def test_count_bearing_answer_passes_recall(self) -> None:
        events = [
            e
            if e.ms != 7000
            else _event(
                7000,
                "recv",
                "agent_response",
                text="I found 3 matches for the playback queue.",
            )
            for e in _happy_events()
        ]
        verdict = BargeInAdjudicator(events).adjudicate()
        assert verdict.criteria[2].passed is True

    def test_missing_probe_answer_fails_recall(self) -> None:
        events = [e for e in _happy_events() if e.ms not in (7000, 9500)]
        verdict = BargeInAdjudicator(events).adjudicate()
        assert verdict.criteria[2].passed is False
        assert "no agent_response" in verdict.criteria[2].evidence

    def test_write_note_error_fails_note_criterion(self) -> None:
        events = [
            e
            if e.ms != 9100
            else _event(
                9100,
                "send",
                "client_tool_result",
                tool="write_note",
                tool_call_id="c2",
                is_error=True,
            )
            for e in _happy_events()
        ]
        verdict = BargeInAdjudicator(events).adjudicate()
        assert verdict.criteria[3].passed is False

    def test_missing_write_note_fails_note_criterion(self) -> None:
        events = [e for e in _happy_events() if e.body.get("tool") != "write_note"]
        verdict = BargeInAdjudicator(events).adjudicate()
        assert verdict.criteria[3].passed is False


class TestJsonlRoundTrip:
    """A trace written by EventTrace parses and adjudicates identically."""

    def test_from_jsonl_matches_in_memory(self, tmp_path: Path) -> None:
        path = tmp_path / "trace.jsonl"
        trace = EventTrace(path)
        for event in _happy_events():
            trace.record(event.direction, event.event_type, dict(event.body))
        verdict = BargeInAdjudicator.from_jsonl(path).adjudicate()
        # The rewritten ms stamps are wall-clock, so ordering (not the
        # absolute values) must produce the same PASS ruling.
        assert verdict.verdict is Verdict.PASSED
