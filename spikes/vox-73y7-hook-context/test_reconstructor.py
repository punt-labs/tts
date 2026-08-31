"""Pins for the ledger-tail context reconstructor (the verdict core).

Deterministic on a fixture ledger shaped like the seeded work session;
sensible on an empty or short tail; unmoved by metadata-only events; and
strict about the cutoff -- the reconstruction must never see past it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from reconstructor import DEFAULT_TAIL_N, TailReconstructor

if TYPE_CHECKING:
    from conftest import RecordFactory
    from stamp import HookRecord

_FAIL_OUTPUT = (
    "test_counts_whitespace_separated_words ... FAIL\n"
    "Traceback (most recent call last):\n"
    "AssertionError: 1 != 4\n"
    "FAILED (failures=1)"
)
_OK_OUTPUT = "Ran 4 tests in 0.001s\nOK"


def _work_session(record: RecordFactory) -> tuple[HookRecord, ...]:
    """A ledger arc shaped like the seeded task: prompt, edit, FAIL, fix, OK."""
    return (
        record(event="SessionStart", payload={"source": "startup"}),
        record(
            event="UserPromptSubmit",
            payload={"prompt": "Run the suite, find the bug, fix it."},
        ),
        record(
            event="PostToolUse",
            payload={
                "tool_name": "Bash",
                "tool_input": {"command": "python3 -m unittest discover"},
                "tool_response": _FAIL_OUTPUT,
            },
        ),
        record(
            event="PostToolUse",
            payload={
                "tool_name": "Edit",
                "tool_input": {"file_path": "/proj/textstat/stats.py"},
                "tool_response": "edited",
            },
        ),
        record(
            event="PostToolUse",
            payload={
                "tool_name": "Bash",
                "tool_input": {"command": "python3 -m unittest discover"},
                "tool_response": _OK_OUTPUT,
            },
        ),
    )


class TestFixtureLedger:
    """Deterministic, field-by-field answer on the work-session arc."""

    def test_full_arc_answer(self, record: RecordFactory) -> None:
        records = _work_session(record)
        answer = TailReconstructor(records, cutoff_index=5).answer("t-final")
        assert answer.goal == "Run the suite, find the bug, fix it."
        assert answer.recent_actions == (
            "Bash: python3 -m unittest discover",
            "Edit: /proj/textstat/stats.py",
            "Bash: python3 -m unittest discover",
        )
        assert "OK" in answer.last_result
        assert answer.open_failure == ""  # the later OK closed it
        assert answer.files_in_play == ("/proj/textstat/stats.py",)

    def test_answer_is_deterministic(self, record: RecordFactory) -> None:
        records = _work_session(record)
        first = TailReconstructor(records, cutoff_index=5).answer("t")
        second = TailReconstructor(records, cutoff_index=5).answer("t")
        assert first == second
        assert first.render() == second.render()

    def test_cutoff_at_the_failure_reports_it_open(self, record: RecordFactory) -> None:
        # Sampled right after the failing run (recv_seq 3): the fix and the
        # green run do not exist yet.
        records = _work_session(record)
        answer = TailReconstructor(records, cutoff_index=3).answer("t-fail")
        assert "AssertionError" in answer.open_failure
        assert answer.files_in_play == ()  # the Edit has not happened yet

    def test_events_after_the_cutoff_are_invisible(self, record: RecordFactory) -> None:
        records = (
            *_work_session(record),
            record(
                event="UserPromptSubmit",
                payload={"prompt": "Now add a readability module."},
            ),
        )
        answer = TailReconstructor(records, cutoff_index=5).answer("t")
        assert answer.goal == "Run the suite, find the bug, fix it."

    def test_failure_stays_open_past_a_neutral_response(
        self, record: RecordFactory
    ) -> None:
        # FAIL, then an Edit whose response is neither green nor failing:
        # nothing has gone green, so the failure is still open.
        records = _work_session(record)[:4]  # ends at the Edit
        answer = TailReconstructor(records, cutoff_index=4).answer("t")
        assert "AssertionError" in answer.open_failure

    def test_dict_wrapped_bash_output_clears_the_failure(
        self, record: RecordFactory
    ) -> None:
        # Claude Code wraps Bash output as {"stdout": ..., "stderr": ...};
        # the success marker must fire on the string leaves, not on a
        # json.dumps whose escaped newlines can never match "\nOK".
        records = (
            *_work_session(record)[:3],  # ends at the FAIL
            record(
                event="PostToolUse",
                payload={
                    "tool_name": "Bash",
                    "tool_input": {"command": "python3 -m unittest discover"},
                    "tool_response": {"stdout": _OK_OUTPUT, "stderr": ""},
                },
            ),
        )
        answer = TailReconstructor(records, cutoff_index=4).answer("t")
        assert answer.open_failure == ""
        assert "OK" in answer.last_result

    def test_agent_report_comes_from_the_newest_stop(
        self, record: RecordFactory
    ) -> None:
        records = (
            *_work_session(record),
            record(
                event="Stop",
                payload={"last_assistant_message": "Fixed the bug; suite is green."},
            ),
        )
        answer = TailReconstructor(records, cutoff_index=6).answer("t")
        assert answer.agent_report == "Fixed the bug; suite is green."
        assert "agent last said: Fixed the bug" in answer.render()

    def test_missing_agent_report_renders_as_nothing_yet(
        self, record: RecordFactory
    ) -> None:
        answer = TailReconstructor(_work_session(record), cutoff_index=5).answer("t")
        assert answer.agent_report == ""
        assert "(nothing yet)" in answer.render()


class TestShortAndEmptyTails:
    """Sensible answers when there is little or nothing to see."""

    def test_empty_ledger_yields_an_empty_but_renderable_answer(self) -> None:
        answer = TailReconstructor((), cutoff_index=99).answer("t0")
        assert answer.goal == ""
        assert answer.recent_actions == ()
        assert answer.open_failure == ""
        rendered = answer.render()
        assert "(unknown)" in rendered
        assert "(none)" in rendered

    def test_cutoff_before_any_event_sees_nothing(self, record: RecordFactory) -> None:
        answer = TailReconstructor(_work_session(record), cutoff_index=0).answer("t0")
        assert answer.goal == ""
        assert answer.recent_actions == ()

    def test_prompt_older_than_the_tail_window_is_forgotten(
        self, record: RecordFactory
    ) -> None:
        # The window is a hard bound: a goal that scrolled out of the last
        # N events is gone -- exactly the degradation the spike measures.
        records = [record(event="UserPromptSubmit", payload={"prompt": "the goal"})]
        records.extend(record(event="Stop") for _ in range(DEFAULT_TAIL_N))
        answer = TailReconstructor(tuple(records), cutoff_index=len(records)).answer(
            "t"
        )
        assert answer.goal == ""


class TestMetadataOnlyEvents:
    """Plumbing events neither crash nor pollute the answer."""

    def test_metadata_only_tail_answers_empty(self, record: RecordFactory) -> None:
        records = (
            record(event="SessionStart"),
            record(event="Notification", payload={"message": "idle"}),
            record(event="Stop"),
            record(event="SessionEnd", payload={"reason": "clear"}),
        )
        answer = TailReconstructor(records, cutoff_index=4).answer("t")
        assert answer.recent_actions == ()
        assert answer.last_result == ""
        assert answer.files_in_play == ()

    def test_tool_use_with_malformed_input_still_lists_the_tool(
        self, record: RecordFactory
    ) -> None:
        rec = record(
            event="PostToolUse",
            payload={"tool_name": "Bash", "tool_input": "not-a-dict"},
        )
        answer = TailReconstructor((rec,), cutoff_index=1).answer("t")
        assert answer.recent_actions == ("Bash",)
