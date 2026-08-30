"""The adjudicator rejects incomplete runs -- pinned offline, no forks.

An ordered, attributed but PARTIAL ledger must fail the hooks criterion
(naming the missing events), and a non-empty but taskless pane must fail
the attach criterion. Both were previously judged only on what arrived.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from run_validation import (
    _HOST_SANITIZER,
    _SCRATCH_ROOT,
    _SURVIVAL_MARKER,
    _SURVIVAL_PROMPT,
    ValidationRun,
)
from stamp import HookLedger, SequenceStamper

if TYPE_CHECKING:
    from pathlib import Path


def _ledger_with(tmp_path: Path, events: list[str]) -> HookLedger:
    ledger = HookLedger(tmp_path / "ledger.jsonl")
    stamper = SequenceStamper()
    for event in events:
        ledger.append(stamper.stamp(event, {"session_id": "sess"}))
    return ledger


class TestJudgeHooksCompleteness:
    """Ordering alone must not pass; the required event set gates first."""

    def test_partial_ledger_fails_and_names_missing_events(
        self, tmp_path: Path
    ) -> None:
        run = ValidationRun()
        ledger = _ledger_with(tmp_path, ["SessionStart"])
        assert run._judge_hooks(ledger) is False
        assert any(
            "missing" in note and "Stop" in note and "UserPromptSubmit" in note
            for note in run._notes
        )

    def test_empty_ledger_fails(self, tmp_path: Path) -> None:
        run = ValidationRun()
        ledger = HookLedger(tmp_path / "ledger.jsonl")
        assert run._judge_hooks(ledger) is False

    def test_complete_ordered_attributed_ledger_passes(self, tmp_path: Path) -> None:
        run = ValidationRun()
        ledger = _ledger_with(
            tmp_path, ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"]
        )
        assert run._judge_hooks(ledger) is True
        assert run._notes == []

    def test_complete_but_unattributed_ledger_fails(self, tmp_path: Path) -> None:
        run = ValidationRun()
        ledger = HookLedger(tmp_path / "ledger.jsonl")
        stamper = SequenceStamper()
        for event in ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"):
            ledger.append(stamper.stamp(event, {}))  # no session_id
        assert run._judge_hooks(ledger) is False

    def test_startup_and_stop_only_ledger_fails_naming_posttooluse(
        self, tmp_path: Path
    ) -> None:
        # Mid-run payload flow is the point of the loopback chain: a fork
        # that emitted only startup and stop hooks proved nothing about
        # it, however ordered and attributed those records are.
        run = ValidationRun()
        ledger = _ledger_with(tmp_path, ["SessionStart", "UserPromptSubmit", "Stop"])
        assert run._judge_hooks(ledger) is False
        assert any("missing" in note and "PostToolUse" in note for note in run._notes)

    def test_judgment_tolerates_an_in_flight_final_line(self, tmp_path: Path) -> None:
        # The store is still serving at judgment time; a hook append can
        # be mid-write during the judge's fresh read. The complete records
        # are judged; the unterminated tail is a write in progress, not
        # corruption.
        run = ValidationRun()
        ledger = _ledger_with(
            tmp_path, ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"]
        )
        with ledger.path.open("a", encoding="utf-8") as handle:
            handle.write('{"recv_seq": 5, "sess')  # torn, no newline
        assert run._judge_hooks(ledger) is True
        assert run._notes == []

    def test_event_landing_after_the_last_poll_still_counts(
        self, tmp_path: Path
    ) -> None:
        # The poll loop's final snapshot is up to one interval stale; a
        # Stop that lands in that gap is in the ledger but not in the
        # snapshot. The judge must read the ledger fresh, or the verdict
        # would contradict the on-disk evidence.
        run = ValidationRun()
        ledger = HookLedger(tmp_path / "ledger.jsonl")
        stamper = SequenceStamper()
        for event in ("SessionStart", "UserPromptSubmit", "PostToolUse"):
            ledger.append(stamper.stamp(event, {"session_id": "sess"}))
        stale_snapshot = {record.event for record in ledger.records()}
        assert "Stop" not in stale_snapshot
        # Stop lands after the caller's last look, same relay stream.
        ledger.append(stamper.stamp("Stop", {"session_id": "sess"}))
        assert run._judge_hooks(ledger) is True
        assert run._notes == []


class TestJudgePane:
    """A usable session shows the seeded task, not merely any content."""

    def test_empty_pane_fails(self) -> None:
        assert ValidationRun()._judge_pane("") is False

    def test_errored_pane_without_task_fails(self) -> None:
        run = ValidationRun()
        pane = "Claude Code v2\nError: something went wrong\n> "
        assert run._judge_pane(pane) is False
        assert any("task marker" in note for note in run._notes)

    def test_pane_showing_the_seeded_task_passes(self) -> None:
        pane = "> create greeting.py defining greet\n* Write(greeting.py)"
        assert ValidationRun()._judge_pane(pane) is True


class TestTeardownLogSanitization:
    """Teardown log lines carry the scratch root; the persisted form
    must not -- the exact substitution the runner applies at write time."""

    def test_teardown_line_with_scratch_root_is_scrubbed(self) -> None:
        raw = f"removed scratch root: {_SCRATCH_ROOT}"
        assert _HOST_SANITIZER.scrub(raw) == "removed scratch root: <scratch>"

    def test_failed_line_with_scratch_root_is_scrubbed(self) -> None:
        raw = f"FAILED to remove scratch root: {_SCRATCH_ROOT}/x"
        scrubbed = _HOST_SANITIZER.scrub(raw)
        assert scrubbed == "FAILED to remove scratch root: <scratch>/x"


class TestSurvivalMarkerNotEchoable:
    """The liveness reply cannot be satisfied by the prompt's own echo.

    send-keys echoes the typed prompt into the pane immediately, so a
    hung fork's pane contains the full prompt text. The marker must
    therefore never be a substring of the prompt -- only a fork that
    actually answered can put it on screen.
    """

    def test_marker_is_absent_from_the_sent_prompt(self) -> None:
        assert _SURVIVAL_MARKER not in _SURVIVAL_PROMPT

    def test_pane_with_only_the_echoed_prompt_does_not_match(self) -> None:
        hung_fork_pane = f"> {_SURVIVAL_PROMPT}\n\n(esc to interrupt)"
        assert _SURVIVAL_MARKER not in hung_fork_pane

    def test_pane_with_a_real_reply_matches(self) -> None:
        answered_pane = f"> {_SURVIVAL_PROMPT}\n\n* ALIVE\n"
        assert _SURVIVAL_MARKER in answered_pane
