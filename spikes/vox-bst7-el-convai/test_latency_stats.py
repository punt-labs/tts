# ruff: noqa: S101 -- pytest asserts; mirrors the repo-wide tests/* per-file-ignore
"""Offline sanity tests for the latency aggregation the spike verdict rests on.

The kill criterion is "p95 tool round-trip < 1.5s". A wrong percentile or a
gate that passes on zero samples corrupts the measurement, so these tests pin
the nearest-rank method and the gate boundary on known inputs.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from control_plane import AgentHandle, ControlPlane
from convai import ConvAISession, EventTrace
from run_automated import LatencyStats, MetricsReport, SeedRun
from spike_tools import ToolBelt

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _invocation(
    tool: str,
    handling: float,
    total: float,
    overhead: float,
    *,
    is_clean: bool = True,
) -> dict[str, object]:
    """Build one invocation record shaped like the per-run metrics output."""
    return {
        "tool": tool,
        "tool_call_id": "call-x",
        "exec_ms": 0.0,
        "handling_ms": handling,
        "total_ms": total,
        "overhead_ms": overhead,
        "is_error": False,
        "is_clean": is_clean,
    }


def _run_record(invocations: list[dict[str, object]]) -> dict[str, object]:
    return {"tag": "t", "seed_bytes": 1024, "invocations": invocations}


class TestLatencyStats:
    """Nearest-rank percentile correctness on known inputs."""

    def test_twenty_samples_known_percentiles(self) -> None:
        # 100..2000 step 100: nearest-rank p50 is the 10th value, p95 the 19th.
        values = [float(v) for v in range(100, 2001, 100)]
        stats = LatencyStats.of(values)
        assert stats.n == 20
        assert stats.p50_ms == 1000.0
        assert stats.p95_ms == 1900.0
        assert stats.max_ms == 2000.0

    def test_single_sample_is_every_percentile(self) -> None:
        stats = LatencyStats.of([137.5])
        assert stats.n == 1
        assert stats.p50_ms == 137.5
        assert stats.p95_ms == 137.5
        assert stats.max_ms == 137.5

    def test_unsorted_input_equals_sorted(self) -> None:
        values = [900.0, 100.0, 500.0, 300.0, 700.0]
        assert LatencyStats.of(values) == LatencyStats.of(sorted(values))

    def test_ties_collapse_to_the_tied_value(self) -> None:
        stats = LatencyStats.of([42.0, 42.0, 42.0, 42.0])
        assert stats.p50_ms == 42.0
        assert stats.p95_ms == 42.0
        assert stats.max_ms == 42.0

    def test_p95_is_an_observed_sample_never_interpolated(self) -> None:
        # Nearest-rank is the defensible small-n method: the percentile is
        # always a real sample, never a value the run did not observe.
        for n in range(1, 8):
            values = [float(100 * (i + 1)) for i in range(n)]
            stats = LatencyStats.of(values)
            assert stats.p95_ms in values
            assert stats.p95_ms <= stats.max_ms

    def test_p95_equals_max_below_twenty_samples(self) -> None:
        # ceil(0.95 * n) == n for all n < 20, so a single outlier dominates
        # small runs -- the conservative property the kill gate relies on.
        values = [10.0] * 5 + [1600.0]
        assert LatencyStats.of(values).p95_ms == 1600.0

    def test_empty_sample_is_all_zeros(self) -> None:
        stats = LatencyStats.of([])
        assert stats.n == 0
        assert stats.p50_ms == 0.0
        assert stats.p95_ms == 0.0
        assert stats.max_ms == 0.0

    def test_as_dict_rounds_to_one_decimal(self) -> None:
        stats = LatencyStats.of([100.06, 100.06, 100.06])
        payload = stats.as_dict()
        assert payload == {
            "n": 3,
            "p50_ms": 100.1,
            "p95_ms": 100.1,
            "max_ms": 100.1,
        }


class TestGateVerdict:
    """The PASS/FAIL kill-criterion gate over aggregated invocations."""

    def test_pass_when_p95_under_threshold(self) -> None:
        invocations = [_invocation("clock", 50.0, 400.0, 350.0) for _ in range(5)]
        report = MetricsReport([_run_record(invocations)])
        assert ": PASS (" in report.gate_verdict()

    def test_fail_when_p95_at_threshold_exactly(self) -> None:
        # The criterion is strict: p95 == 1500 is a FAIL, not a pass.
        invocations = [_invocation("clock", 50.0, 1550.0, 1500.0) for _ in range(3)]
        report = MetricsReport([_run_record(invocations)])
        assert ": FAIL (" in report.gate_verdict()

    def test_fail_on_zero_samples_despite_zero_p95(self) -> None:
        # LatencyStats.of([]) reports p95 == 0.0 < 1500; the n > 0 guard must
        # keep an empty run from silently passing the spike.
        report = MetricsReport([_run_record([])])
        assert ": FAIL (" in report.gate_verdict()

    def test_unclean_samples_excluded_from_the_gate_metric(self) -> None:
        # An invocation co-scheduled with another tool measures our own
        # sleep, not EL: it must leave overhead_ms but stay in the
        # handling/total tables. A huge dirty overhead must not flip a
        # clean run to FAIL.
        clean = _invocation("clock", 50.0, 400.0, 350.0)
        dirty = _invocation("clock", 50.0, 5000.0, 4950.0, is_clean=False)
        report = MetricsReport([_run_record([clean, dirty])])
        assert ": PASS (" in report.gate_verdict()

    def test_all_unclean_run_cannot_pass_the_gate(self) -> None:
        # If every sample was contaminated by co-scheduling, there is no
        # EL-attributable evidence at all: n == 0 must FAIL, not pass on
        # the zero sentinel.
        dirty = _invocation("clock", 50.0, 400.0, 350.0, is_clean=False)
        report = MetricsReport([_run_record([dirty])])
        assert ": FAIL (" in report.gate_verdict()

    def test_single_outlier_fails_a_small_run(self) -> None:
        invocations = [
            _invocation("clock", 50.0, 150.0, 100.0),
            _invocation("clock", 50.0, 160.0, 110.0),
            _invocation("search_code", 3000.0, 4700.0, 1700.0),
        ]
        report = MetricsReport([_run_record(invocations)])
        assert ": FAIL (" in report.gate_verdict()


class TestAggregation:
    """Run records aggregate across runs and split per tool."""

    def test_save_aggregates_overall_and_per_tool(self, tmp_path: Path) -> None:
        run_a = _run_record(
            [
                _invocation("clock", 50.0, 300.0, 250.0),
                _invocation("search_code", 2200.0, 3400.0, 1200.0),
            ]
        )
        run_b = _run_record([_invocation("clock", 60.0, 500.0, 440.0)])
        errored_run: dict[str, object] = {"tag": "err", "error": "TimeoutError: x"}
        report = MetricsReport([run_a, run_b, errored_run])
        out = tmp_path / "metrics.json"
        report.save(out)
        payload = json.loads(out.read_text(encoding="utf-8"))
        overall = payload["aggregate"]["overall"]
        assert overall["overhead_ms"]["n"] == 3
        assert overall["overhead_ms"]["max_ms"] == 1200.0
        per_tool = payload["aggregate"]["per_tool"]
        assert set(per_tool) == {"clock", "search_code"}
        assert per_tool["clock"]["overhead_ms"]["n"] == 2
        assert per_tool["search_code"]["total_ms"]["max_ms"] == 3400.0

    def test_unclean_samples_stay_in_handling_and_total(self, tmp_path: Path) -> None:
        clean = _invocation("clock", 50.0, 400.0, 350.0)
        dirty = _invocation("clock", 60.0, 5000.0, 4950.0, is_clean=False)
        report = MetricsReport([_run_record([clean, dirty])])
        out = tmp_path / "metrics.json"
        report.save(out)
        overall = json.loads(out.read_text(encoding="utf-8"))["aggregate"]["overall"]
        assert overall["handling_ms"]["n"] == 2  # dirty sample still described
        assert overall["total_ms"]["n"] == 2
        assert overall["overhead_ms"]["n"] == 1  # but excluded from the gate metric
        assert overall["overhead_ms"]["max_ms"] == 350.0

    def test_table_lists_every_metric_row(self) -> None:
        report = MetricsReport([_run_record([_invocation("clock", 1.0, 2.0, 1.0)])])
        table = report.table()
        for metric in ("handling_ms", "total_ms", "overhead_ms"):
            assert f"overall {metric}" in table
            assert f"clock {metric}" in table


class TestCollectRecord:
    """The per-run record fields that ride into the metrics JSON."""

    @staticmethod
    def _collect_record(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        turn_response_ms: list[float],
    ) -> dict[str, object]:
        """Collect a run record from a never-opened session, fully offline."""
        monkeypatch.setenv("ELEVENLABS_API_KEY", "offline-test-key")
        plane = ControlPlane()  # constructs an HTTP client; no request is made
        try:
            run = SeedRun(
                plane=plane,
                handle=AgentHandle(agent_id="agent-test", tool_ids=()),
                seed_bytes=1024,
                turns=(),
                tag="collect-test",
            )
            session = ConvAISession(
                url="ws://unused.invalid",
                toolbelt=ToolBelt(tmp_path / "notes.txt"),
                trace=EventTrace(tmp_path / "trace.jsonl"),
                overrides={},
            )
            session.metrics.turn_response_ms.extend(turn_response_ms)
            # The record shape is the seam under test; there is no public
            # entry that reaches it without a live EL conversation.
            return run._collect(session)
        finally:
            plane.close()

    def test_zero_first_response_survives_as_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 0.0 is a measurement, not absence: truthiness must not turn a
        # legitimate instant first response into a missing sample.
        record = self._collect_record(tmp_path, monkeypatch, [0.0])
        assert record["first_response_ms"] == 0.0

    def test_no_turns_reports_first_response_as_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Absence stays None only when the run truly produced no turn.
        record = self._collect_record(tmp_path, monkeypatch, [])
        assert record["first_response_ms"] is None
