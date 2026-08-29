# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx>=0.27", "websockets>=14"]
# ///
"""Automated text-mode harness: tool round-trip latency + seed-size push.

Run from this directory (after setup_agent.py):

    direnv exec ../../ uv run run_automated.py            # full: 3 seed sizes
    direnv exec ../../ uv run run_automated.py --smoke    # 1 short run

Each run opens one text-only Conv AI session, overrides the agent prompt
with BASE_PROMPT + a generated seed of the target size, drives the scripted
turns, and answers every client_tool_call locally. Everything lands in
results/: an event-trace JSONL per run and one metrics JSON per invocation
of this script.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self, final

from control_plane import AgentHandle, ControlPlane
from convai import ConvAISession, EventTrace
from seed import SeedGenerator
from setup_agent import BASE_PROMPT, LLM_ID
from spike_tools import ToolBelt

_HERE = Path(__file__).parent
_RESULTS = _HERE / "results"

type _StatsByMetric = dict[str, LatencyStats]

_SEED_SIZES: tuple[int, ...] = (1_024, 10_240, 51_200)

# Seven turns eliciting ~9 tool invocations; three seed runs -> ~27 total.
_TURNS: tuple[str, ...] = (
    "What time is it right now?",
    "Search the code for the playback queue.",
    "Write a note saying: spike checkpoint alpha.",
    "Check the clock and also search the code for provider registry.",
    (
        "Search the code for websocket dispatch, and write a note that "
        "says: dispatch reviewed."
    ),
    "What day is it today? Also note down: seed run complete.",
    "Thanks. Give me a one-sentence summary of what you did.",
)

_SMOKE_TURNS: tuple[str, ...] = (
    "What time is it right now?",
    "Write a note saying: smoke test note.",
)


@dataclass(frozen=True, slots=True)
class LatencyStats:
    """Nearest-rank percentile summary of a latency sample."""

    n: int
    p50_ms: float
    p95_ms: float
    max_ms: float

    @classmethod
    def of(cls, values: Sequence[float]) -> Self:
        if not values:
            return cls(n=0, p50_ms=0.0, p95_ms=0.0, max_ms=0.0)
        ordered = sorted(values)
        return cls(
            n=len(ordered),
            p50_ms=cls._rank(ordered, 0.50),
            p95_ms=cls._rank(ordered, 0.95),
            max_ms=ordered[-1],
        )

    @staticmethod
    def _rank(ordered: Sequence[float], p: float) -> float:
        index = max(math.ceil(p * len(ordered)) - 1, 0)
        return ordered[index]

    def as_dict(self) -> dict[str, object]:
        return {
            "n": self.n,
            "p50_ms": round(self.p50_ms, 1),
            "p95_ms": round(self.p95_ms, 1),
            "max_ms": round(self.max_ms, 1),
        }

    def row(self, label: str) -> str:
        return (
            f"{label:<28} {self.n:>3} {self.p50_ms:>9.0f} "
            f"{self.p95_ms:>9.0f} {self.max_ms:>9.0f}"
        )


@final
class SeedRun:
    """One text-only session at one seed size, driving the scripted turns."""

    _plane: ControlPlane
    _handle: AgentHandle
    _seed_bytes: int
    _turns: tuple[str, ...]
    _tag: str

    def __new__(
        cls,
        *,
        plane: ControlPlane,
        handle: AgentHandle,
        seed_bytes: int,
        turns: tuple[str, ...],
        tag: str,
    ) -> Self:
        self = super().__new__(cls)
        self._plane = plane
        self._handle = handle
        self._seed_bytes = seed_bytes
        self._turns = turns
        self._tag = tag
        return self

    async def execute(self) -> dict[str, object]:
        """Run the session end to end; return the per-run record."""
        seed_text = SeedGenerator().generate(self._seed_bytes)
        prompt = BASE_PROMPT + "\n\n" + seed_text
        trace = EventTrace(_RESULTS / f"trace_{self._tag}.jsonl")
        trace.record(
            "note",
            "run_config",
            {"seed_bytes": self._seed_bytes, "prompt_bytes": len(prompt.encode())},
        )
        record: dict[str, object] = {
            "tag": self._tag,
            "seed_bytes": self._seed_bytes,
            "prompt_bytes": len(prompt.encode()),
        }
        session: ConvAISession | None = None  # None when signed_url fails
        try:
            t0 = time.monotonic()
            url = self._plane.signed_url(self._handle.agent_id)
            record["signed_url_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
            session = ConvAISession(
                url=url,
                toolbelt=ToolBelt(_HERE / "notes.txt"),
                trace=trace,
                overrides={
                    "agent": {"prompt": {"prompt": prompt}},
                    "conversation": {"text_only": True},
                },
            )
            await session.open()
            await self._drive_turns(session, trace)
        except (TimeoutError, OSError, RuntimeError) as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            trace.record("note", "run_error", {"error": str(exc)})
        finally:
            if session is not None:
                await session.close()
        if session is None:
            return record
        return {**record, **self._collect(session)}

    async def _drive_turns(self, session: ConvAISession, trace: EventTrace) -> None:
        for turn in self._turns:
            try:
                reply = await session.say(turn)
            except TimeoutError as exc:
                trace.record("note", "turn_timeout", {"turn": turn})
                reply = f"<timeout: {exc}>"
            print(f"    you>   {turn}")
            print(f"    agent> {reply}")

    def _collect(self, session: ConvAISession) -> dict[str, object]:
        metrics = session.metrics
        completed = metrics.completed_invocations
        session_start_ms = metrics.ws_connect_ms + metrics.init_metadata_ms
        first_ms = (
            metrics.turn_response_ms[0] if metrics.turn_response_ms else None
        )  # None only when the run errored before any turn completed
        return {
            "conversation_id": session.conversation_id,
            "ws_connect_ms": round(metrics.ws_connect_ms, 1),
            "init_metadata_ms": round(metrics.init_metadata_ms, 1),
            "session_start_ms": round(session_start_ms, 1),
            "first_response_ms": round(first_ms, 1) if first_ms else None,
            "turn_response_ms": [round(v, 1) for v in metrics.turn_response_ms],
            "ping_ms": metrics.ping_ms,
            "invocations": [inv.as_dict() for inv in completed],
            "incomplete_invocations": len(metrics.invocations) - len(completed),
            "transcript": metrics.transcript,
        }


@final
class MetricsReport:
    """Aggregate run records into percentile tables and a metrics JSON."""

    _runs: list[dict[str, object]]

    def __new__(cls, runs: list[dict[str, object]]) -> Self:
        self = super().__new__(cls)
        self._runs = runs
        return self

    def save(self, path: Path) -> None:
        overall, per_tool = self._aggregate()
        payload = {
            "generated": datetime.now(tz=UTC).isoformat(timespec="seconds"),
            "llm": LLM_ID,
            "gate": "overall overhead_ms p95 < 1500",
            "runs": self._runs,
            "aggregate": {
                "overall": {m: s.as_dict() for m, s in overall.items()},
                "per_tool": {
                    tool: {m: s.as_dict() for m, s in metrics.items()}
                    for tool, metrics in per_tool.items()
                },
            },
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def table(self) -> str:
        lines = [
            f"{'metric':<28} {'n':>3} {'p50':>9} {'p95':>9} {'max':>9}",
            "-" * 62,
        ]
        overall, per_tool = self._aggregate()
        for metric, stats in overall.items():
            lines.append(stats.row(f"overall {metric}"))
        for tool, per_metric in per_tool.items():
            lines.extend(
                stats.row(f"{tool} {metric}") for metric, stats in per_metric.items()
            )
        return "\n".join(lines)

    def gate_verdict(self) -> str:
        overall, _ = self._aggregate()
        stats = overall["overhead_ms"]
        verdict = "PASS" if stats.p95_ms < 1500.0 and stats.n > 0 else "FAIL"
        return (
            f"p95 tool round-trip (EL-attributable overhead) < 1.5s: "
            f"{verdict} (p95={stats.p95_ms:.0f}ms over n={stats.n})"
        )

    def _invocations(self) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for run in self._runs:
            invocations = run.get("invocations")
            if isinstance(invocations, list):
                out.extend(invocations)
        return out

    def _aggregate(self) -> tuple[_StatsByMetric, dict[str, _StatsByMetric]]:
        invocations = self._invocations()
        tools = sorted({str(inv["tool"]) for inv in invocations})
        per_tool = {
            tool: self._stats_for([inv for inv in invocations if inv["tool"] == tool])
            for tool in tools
        }
        return self._stats_for(invocations), per_tool

    @staticmethod
    def _stats_for(invocations: list[dict[str, object]]) -> dict[str, LatencyStats]:
        # overhead_ms is EL-attributable only for "clean" invocations --
        # ones whose result was the last thing the agent waited on. A tool
        # co-scheduled with our own slow tool measures that tool, not EL.
        clean = [inv for inv in invocations if inv.get("is_clean", True)]
        stats = {
            metric: LatencyStats.of([float(str(inv[metric])) for inv in invocations])
            for metric in ("handling_ms", "total_ms")
        }
        stats["overhead_ms"] = LatencyStats.of(
            [float(str(inv["overhead_ms"])) for inv in clean]
        )
        return stats


def main() -> None:
    """Drive the seed runs and write the metrics artifacts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true", help="one short run")
    parser.add_argument(
        "--sizes",
        type=int,
        nargs="*",
        default=list(_SEED_SIZES),
        help="seed sizes in bytes",
    )
    args = parser.parse_args()
    sizes = [1_024] if args.smoke else list(args.sizes)
    turns = _SMOKE_TURNS if args.smoke else _TURNS
    handle = AgentHandle.load(_HERE / "agent.json")
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    plane = ControlPlane()
    runs: list[dict[str, object]] = []
    try:
        for size in sizes:
            tag = f"{stamp}_seed{size}"
            print(f"== seed {size} bytes ({tag}) ==")
            run = SeedRun(
                plane=plane,
                handle=handle,
                seed_bytes=size,
                turns=turns,
                tag=tag,
            )
            runs.append(asyncio.run(run.execute()))
    finally:
        plane.close()
    report = MetricsReport(runs)
    metrics_path = _RESULTS / f"metrics_{stamp}.json"
    report.save(metrics_path)
    print()
    print(report.table())
    print()
    print(report.gate_verdict())
    print(f"metrics: {metrics_path}")


if __name__ == "__main__":
    main()
