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
from run_arm2 import Arm2Runner, StoreProcess

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
        stub_lines, _teardown_lines = runner.teardown_with_evidence(store)
        assert any("vox-panel" in line for line in stub_lines)
        assert any("--probe-hit" in line for line in stub_lines)
        assert not scratch.exists()

    def test_no_hits_reads_as_observed_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scratch = tmp_path / "scratch"
        monkeypatch.setattr(run_arm2, "_SCRATCH_ROOT", scratch)
        runner = Arm2Runner()
        runner.stubs.create()
        store = StoreProcess(1, tmp_path / "ledger.jsonl")
        stub_lines, _teardown_lines = runner.teardown_with_evidence(store)
        assert stub_lines == []
        assert not scratch.exists()
