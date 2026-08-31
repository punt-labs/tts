"""Pins for the Arm 2 runner's evidence-before-destruction ordering.

The first live run's summary claimed "zero sentinel-stub hits" while the
harvest ran AFTER teardown had removed the log — the claim was true by
construction, not observation. These tests pin the fixed contract: a stub
hit that happened MUST surface in what teardown returns, and the harvest
happens before the scratch root is removed.

Needs claude/tmux/mcp-proxy on PATH (Arm2Runner refuses otherwise) but
spawns none of them — the store is never started and no session exists.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest

import run_arm2
from run_arm2 import Arm2Runner, CaseResult, StoreProcess

if TYPE_CHECKING:
    from pathlib import Path


class TestRerunSafety:
    """A rerun over committed evidence must refuse, not interleave."""

    def test_run_refuses_a_preexisting_ledger(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The store APPENDS; starting a new run over an existing ledger
        # makes every receipt wait match the previous run's records
        # (observed live: negative latencies, stale recv_seqs). The run
        # must die before anything spawns.
        monkeypatch.setattr(run_arm2, "_SCRATCH_ROOT", tmp_path / "scratch")
        monkeypatch.setattr(run_arm2, "_RESULTS", tmp_path / "results")
        (tmp_path / "results").mkdir()
        (tmp_path / "results" / "hook_ledger.jsonl").write_text(
            "{}\n", encoding="utf-8"
        )
        with pytest.raises(RuntimeError, match="ledger already exists"):
            Arm2Runner().run()


class TestTeardownHarvestsFirst:
    """Stub evidence is read before anything is destroyed."""

    def test_recorded_hit_survives_teardown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scratch = tmp_path / "scratch"
        monkeypatch.setattr(run_arm2, "_SCRATCH_ROOT", scratch)
        runner = Arm2Runner()
        stubs = runner.stubs
        stubs.create()
        env = dict(os.environ)
        env["PATH"] = stubs.path_env(env["PATH"])
        subprocess.run(
            ["vox-panel", "--probe-hit"], env=env, check=True, capture_output=True
        )
        store = StoreProcess(1, tmp_path / "ledger.jsonl")  # never started
        stub_lines, _teardown_lines, clean = runner.teardown_with_evidence(store)
        assert any("vox-panel" in line for line in stub_lines)
        assert any("--probe-hit" in line for line in stub_lines)
        assert clean is True
        assert not scratch.exists()

    def test_no_hits_reads_as_observed_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scratch = tmp_path / "scratch"
        monkeypatch.setattr(run_arm2, "_SCRATCH_ROOT", scratch)
        runner = Arm2Runner()
        runner.stubs.create()
        store = StoreProcess(1, tmp_path / "ledger.jsonl")
        stub_lines, _teardown_lines, clean = runner.teardown_with_evidence(store)
        assert stub_lines == []
        assert clean is True
        assert not scratch.exists()


class TestTeardownResidueIsNotClean:
    """A teardown that leaves residue must surface clean=False."""

    def test_dirty_teardown_pass_reports_not_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scratch = tmp_path / "scratch"
        monkeypatch.setattr(run_arm2, "_SCRATCH_ROOT", scratch)
        runner = Arm2Runner()
        runner.stubs.create()

        class DirtyTeardown:
            def __init__(self, _root: Path) -> None: ...

            def run(self) -> object:
                # The worst residue: a credentials copy that survived.
                return type(
                    "Outcome", (), {"clean": False, "log": ("FAILED to remove",)}
                )()

        monkeypatch.setattr(run_arm2, "Teardown", DirtyTeardown)
        store = StoreProcess(1, tmp_path / "ledger.jsonl")
        _stub_lines, _lines, clean = runner.teardown_with_evidence(store)
        assert clean is False


class TestMissingCaptureIsExplicit:
    """A dead session's pane capture is written down, not omitted."""

    def test_capture_with_no_session_writes_a_marker_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(run_arm2, "_SCRATCH_ROOT", tmp_path / "scratch")
        monkeypatch.setattr(run_arm2, "_RESULTS", tmp_path / "results")
        (tmp_path / "results").mkdir()
        runner = Arm2Runner()
        runner._capture("dead_case")
        marker = (tmp_path / "results" / "pane_dead_case.txt").read_text("utf-8")
        assert "unavailable" in marker


class TestCaseGuardCoversSubprocessFaults:
    """A tmux failure mid-case is the case's finding, never a run abort."""

    def test_called_process_error_becomes_the_case_error(self) -> None:
        def tmux_blew_up() -> CaseResult:
            raise subprocess.CalledProcessError(1, ["tmux", "send-keys"])

        result = Arm2Runner._guarded_case("boom", tmux_blew_up)
        assert result.name == "boom"
        assert "tmux" in str(result.summary["error"])

    def test_a_harness_bug_still_escapes(self) -> None:
        def broken_harness() -> CaseResult:
            msg = "not a live-session fault"
            raise ValueError(msg)

        with pytest.raises(ValueError, match="not a live-session fault"):
            Arm2Runner._guarded_case("bug", broken_harness)


class TestExitCodeGatesOnResidue:
    """Case errors and on-disk residue both fail the run."""

    def test_dirty_teardown_fails_a_run_with_clean_cases(self) -> None:
        results = [CaseResult("fine", {"ok": True})]
        assert Arm2Runner._exit_code(results, teardown_clean=False) == 1

    def test_clean_cases_and_clean_teardown_pass(self) -> None:
        results = [CaseResult("fine", {"ok": True})]
        assert Arm2Runner._exit_code(results, teardown_clean=True) == 0

    def test_case_error_fails_even_a_clean_teardown(self) -> None:
        results = [CaseResult("boom", {"error": "x"})]
        assert Arm2Runner._exit_code(results, teardown_clean=True) == 1
