"""Pins for the Arm 1 runner's error disposal — no pi, no spend.

The failure family under test: a scenario that dies mid-way (a marker
that never came, a hung turn) must still yield its REAL transcript, its
child's exit code, and an honest summary — never an empty evidence file
or a flag rewritten by a later timeout.
"""

from __future__ import annotations

import json
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

import run_arm1
from rpc_protocol import RpcCommand, Transcript
from rpc_session import PiRpcSession
from run_arm1 import Arm1Runner, ScenarioOutcome, _probe
from steer_analysis import TranscriptAnalysis

if TYPE_CHECKING:
    from pathlib import Path

# A peer that acks a steer and STARTS an agent loop but never ends it —
# the started-then-hung idle case.
_HANGING_PEER = textwrap.dedent(
    """\
    import json, sys

    for line in sys.stdin:
        command = json.loads(line)
        kind = command["type"]
        print(
            json.dumps({"type": "response", "command": kind, "success": True}),
            flush=True,
        )
        if kind == "steer":
            print(json.dumps({"type": "agent_start"}), flush=True)
    """
)


def _peer_session(tmp_path: Path, body: str) -> PiRpcSession:
    script = tmp_path / "peer.py"
    script.write_text(body, encoding="utf-8")
    return PiRpcSession.spawn(
        [sys.executable, str(script)],
        cwd=tmp_path,
        stderr_path=tmp_path / "peer_stderr.log",
    )


def _runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Arm1Runner:
    monkeypatch.setattr(run_arm1, "SCRATCH_ROOT", tmp_path / "scratch")
    monkeypatch.setattr(run_arm1, "RESULTS_ROOT", tmp_path / "results")
    return Arm1Runner("plain")


class TestGuardedKeepsTheEvidence:
    """A failing scenario still yields its real transcript + exit code."""

    def test_error_outcome_carries_the_real_transcript(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _runner(tmp_path, monkeypatch)
        session = _peer_session(tmp_path, _HANGING_PEER)
        monkeypatch.setattr(Arm1Runner, "_session", lambda _self, _name: session)

        def failing(live: PiRpcSession) -> dict[str, object]:
            live.send(RpcCommand.steer("go left"))
            live.wait_for(
                lambda e: e.is_response_to("steer"),
                timeout_s=10,
                description="steer ack",
            )
            msg = "no event matched: queue_update"
            raise LookupError(msg)

        outcome = runner._guarded("boom", failing)
        assert "no event matched" in str(outcome.summary["error"])
        # The live exchange survives into the evidence dump.
        assert any('"steer"' in entry.text for entry in outcome.transcript.entries())
        assert outcome.summary["pi_exit_code"] == 0

    def test_successful_outcome_records_the_exit_code_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _runner(tmp_path, monkeypatch)
        session = _peer_session(tmp_path, _HANGING_PEER)
        monkeypatch.setattr(Arm1Runner, "_session", lambda _self, _name: session)
        outcome = runner._guarded("fine", lambda _live: {"ok": True})
        assert outcome.summary == {"ok": True, "pi_exit_code": 0}


class TestSpawnFailureIsAScenarioOutcome:
    """A session that never spawns is that scenario's finding, not a crash."""

    def test_spawn_failure_yields_an_error_outcome(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _runner(tmp_path, monkeypatch)

        def refuse(_self: Arm1Runner, _name: str) -> PiRpcSession:
            msg = "spawn refused: transient resource exhaustion"
            raise OSError(msg)

        monkeypatch.setattr(Arm1Runner, "_session", refuse)
        outcome = runner._guarded("never_spawned", lambda _s: {"ok": True})
        assert "spawn refused" in str(outcome.summary["error"])
        assert outcome.transcript.entries() == ()  # honest: nothing happened
        assert outcome.summary["pi_exit_code"] is None

    def test_later_scenarios_still_execute_after_a_spawn_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _runner(tmp_path, monkeypatch)
        working = _peer_session(tmp_path, _HANGING_PEER)
        calls: list[str] = []

        def flaky(_self: Arm1Runner, name: str) -> PiRpcSession:
            calls.append(name)
            if name == "first":
                msg = "spawn refused once"
                raise OSError(msg)
            return working

        monkeypatch.setattr(Arm1Runner, "_session", flaky)
        first = runner._guarded("first", lambda _s: {"ok": True})
        second = runner._guarded("second", lambda _s: {"ok": True})
        assert "error" in first.summary
        assert second.summary == {"ok": True, "pi_exit_code": 0}
        assert calls == ["first", "second"]


class TestIdleSteerHungTurn:
    """A started-then-hung turn is reported as exactly that."""

    def test_started_flag_stands_and_no_prompt_is_injected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _runner(tmp_path, monkeypatch)
        monkeypatch.setattr(run_arm1, "_IDLE_PROBE_S", 5)
        monkeypatch.setattr(run_arm1, "_TURN_WAIT_S", 0.5)
        session = _peer_session(tmp_path, _HANGING_PEER)
        try:
            summary = runner._idle_steer(session)
        finally:
            session.close()
        assert summary["idle_steer_started_a_turn"] is True
        assert "hung" in str(summary["error"])
        # No fresh prompt was typed into the still-running turn.
        sends = [
            json.loads(entry.text)["type"]
            for entry in session.transcript.entries()
            if entry.direction == "send"
        ]
        assert sends == ["steer"]


class TestInFlightToolSchema:
    """A missing toolCallId must fail loudly, not fabricate a completion."""

    def test_idless_tool_start_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _runner(tmp_path, monkeypatch)
        transcript = Transcript()
        transcript.note_recv('{"type": "tool_execution_start"}', ns=1)
        transcript.note_recv('{"type": "tool_execution_end"}', ns=2)
        analysis = TranscriptAnalysis(transcript)
        with pytest.raises(LookupError, match="toolCallId"):
            runner._in_flight_tool(analysis, steer_ns=0)


class TestProbe:
    """Provenance probes record failure instead of raising or blanking."""

    def test_failing_probe_records_the_failure(self) -> None:
        assert _probe(["false"]).startswith("<probe failed rc=1")

    def test_successful_probe_returns_stdout(self) -> None:
        assert _probe(["echo", "v1.2.3"]) == "v1.2.3"


class TestPreserveStderr:
    """A failed scenario's pi stderr survives into the evidence dir."""

    def test_failed_scenario_stderr_is_copied_and_sanitized(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _runner(tmp_path, monkeypatch)
        scratch = run_arm1.SCRATCH_ROOT / "boom"
        scratch.mkdir(parents=True)
        leaky = f"panic: cannot open {run_arm1.SCRATCH_ROOT}/boom/notes_01.txt"
        (scratch / "pi_stderr.log").write_text(leaky + "\n", encoding="utf-8")
        runner._results_dir.mkdir(parents=True)
        outcome = ScenarioOutcome(
            name="boom", summary={"error": "x"}, transcript=Transcript()
        )
        runner._preserve_stderr([outcome])
        copied = (runner._results_dir / "boom.pi_stderr.log").read_text("utf-8")
        assert "panic: cannot open" in copied
        assert str(run_arm1.SCRATCH_ROOT) not in copied  # sanitized
        assert "<scratch>" in copied

    def test_missing_stderr_is_an_explicit_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _runner(tmp_path, monkeypatch)
        runner._results_dir.mkdir(parents=True)
        outcome = ScenarioOutcome(
            name="ghost", summary={"error": "x"}, transcript=Transcript()
        )
        runner._preserve_stderr([outcome])
        marker = (runner._results_dir / "ghost.pi_stderr.log").read_text("utf-8")
        assert "never created" in marker

    def test_successful_scenarios_leave_no_stderr_copies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _runner(tmp_path, monkeypatch)
        runner._results_dir.mkdir(parents=True)
        outcome = ScenarioOutcome(name="fine", summary={}, transcript=Transcript())
        runner._preserve_stderr([outcome])
        assert not list(runner._results_dir.glob("*.pi_stderr.log"))
