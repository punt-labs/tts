# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""Arm 1 live run: steer a running `pi --mode rpc` turn, three scenarios.

1. **midturn_steer** — a long multi-step read task; `steer` lands after the
   first tool call starts. Characterizes interrupt-vs-queue, the in-flight
   tool call's fate, stream coherence after, and steer-write latencies.
2. **idle_steer** — `steer` with no turn running: does it start a turn or
   sit queued, and what delivers it?
3. **followup_contrast** — same long task, `follow_up` instead of `steer`.

Isolation: the pi child runs in a fresh scratch project outside the repo,
with sentinel `vox`/`vox-panel` stubs first on PATH; tools are read-only.
Evidence: one sanitized transcript per scenario plus a summary, written
under results/arm1-<steer style>/. Run from this directory:

    direnv exec ../../ uv run run_arm1.py --steer-style plain
    direnv exec ../../ uv run run_arm1.py --steer-style adversarial
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Self, final

from rpc_protocol import RpcCommand, RpcEvent, Transcript
from rpc_session import PiRpcSession, PiSpec
from stamp import Sanitizer
from steer_analysis import TranscriptAnalysis
from stubs import SentinelStubs

SCRATCH_ROOT = Path.home() / ".cache" / "vox04qy-scratch" / "arm1"
RESULTS_ROOT = Path(__file__).parent / "results"

_SEEDED_FILES = 6
_TOOL_WAIT_S = 90
_ACK_WAIT_S = 15
_TURN_WAIT_S = 240
_IDLE_PROBE_S = 10

_LONG_TASK = (
    f"Read each of the {_SEEDED_FILES} files notes_01.txt through "
    f"notes_0{_SEEDED_FILES}.txt in this directory using the read tool, "
    "strictly one at a time in ascending order. After each file, write a "
    "two-sentence summary of it before reading the next. Never read more "
    "than one file per tool call."
)
# Two steer phrasings, because the FIRST live run showed the channel and
# the model are separate questions: protocol-styled "URGENT STEER" text
# was delivered mid-turn in ~7ms and then REFUSED by the model as a
# suspected prompt injection, while the same request phrased as a plain
# user sentence is the compliance case. Both stay runnable.
_STEER_TEXTS: dict[str, str] = {
    "adversarial": (
        "URGENT STEER vox04qy: stop the summaries immediately. Reply with "
        "exactly STEERED-ACK and nothing else, then stop."
    ),
    "plain": (
        "Actually, please stop — I don't need the rest of the summaries. "
        "Just reply with exactly STEERED-ACK and nothing else."
    ),
}
_IDLE_STEER_TEXT = (
    "IDLE STEER vox04qy: reply with exactly IDLESTEER-ACK and nothing else."
)
_PROMPT_AFTER_IDLE = "Reply with exactly PROMPT-ACK and nothing else."
_FOLLOWUP_TEXT = "FOLLOW-UP vox04qy: reply with exactly FOLLOWUP-ACK and nothing else."


# Everything a live scenario can legitimately fail with; anything outside
# this set is a harness bug and should crash loudly. RuntimeError covers
# the session's own loud faults (fork died at startup, torn reader).
_SCENARIO_FAULTS = (
    TimeoutError,
    LookupError,
    OSError,
    subprocess.SubprocessError,
    RuntimeError,
)


def _probe(argv: list[str]) -> str:
    """A version/provenance probe that records failure instead of raising.

    Provenance must never abort a run after the scenarios completed, and
    an empty string is not a recording of what went wrong.
    """
    result = subprocess.run(argv, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        return f"<probe failed rc={result.returncode}: {detail[:80]}>"
    return result.stdout.strip()


def _assistant_marker(marker: str) -> str:
    """Name for the predicate below, kept next to it for the report."""
    return f"assistant text containing {marker}"


def _emits(marker: str) -> Callable[[RpcEvent], bool]:
    """Predicate: the ASSISTANT (not the echoed user message) says marker."""

    def predicate(event: RpcEvent) -> bool:
        return event.contains(marker) and event.contains('"role":"assistant"')

    return predicate


@final
@dataclass(frozen=True, slots=True)
class ScenarioOutcome:
    """One scenario's transcript plus the derived summary block."""

    name: str
    summary: dict[str, object]
    transcript: Transcript


@final
class Arm1Runner:
    """Spawns one pi RPC session per scenario and records the evidence."""

    __slots__ = ("_results_dir", "_sanitizer", "_spec", "_steer_text", "_stubs")

    _results_dir: Path
    _sanitizer: Sanitizer
    _spec: PiSpec
    _steer_text: str
    _stubs: SentinelStubs

    def __new__(cls, steer_style: str) -> Self:
        pi_binary = shutil.which("pi")
        if pi_binary is None:
            msg = "pi is not on PATH"
            raise RuntimeError(msg)
        if steer_style not in _STEER_TEXTS:
            msg = f"unknown steer style: {steer_style}"
            raise ValueError(msg)
        self = super().__new__(cls)
        self._steer_text = _STEER_TEXTS[steer_style]
        self._results_dir = RESULTS_ROOT / f"arm1-{steer_style}"
        self._spec = PiSpec(
            binary=Path(pi_binary),
            provider="anthropic",
            model="claude-haiku-4-5",
            tools=("read", "grep", "find", "ls"),
        )
        self._stubs = SentinelStubs(SCRATCH_ROOT / "stubs")
        self._sanitizer = Sanitizer.for_host(SCRATCH_ROOT)
        return self

    def run(self) -> int:
        """All three scenarios; returns a process exit code."""
        # Results dir and environment provenance come FIRST: nothing that
        # can fail at the end of the run may stand between a finished
        # scenario and its evidence on disk.
        self._results_dir.mkdir(parents=True, exist_ok=True)
        environment = self._environment()
        self._stubs.create()
        outcomes = [
            self._guarded("midturn_steer", self._midturn_steer),
            self._guarded("idle_steer", self._idle_steer),
            self._guarded("followup_contrast", self._followup_contrast),
        ]
        summary: dict[str, object] = {
            "environment": environment,
            "scenarios": {outcome.name: outcome.summary for outcome in outcomes},
            "stub_invocations": list(self._stubs.invocations()),
        }
        for outcome in outcomes:
            outcome.transcript.dump(
                self._results_dir / f"{outcome.name}.transcript.jsonl",
                self._sanitizer,
            )
        self._preserve_stderr(outcomes)
        summary_path = self._results_dir / "summary.json"
        summary_path.write_text(
            self._sanitizer.scrub(json.dumps(summary, indent=2, sort_keys=True)) + "\n",
            encoding="utf-8",
        )
        shutil.rmtree(SCRATCH_ROOT, ignore_errors=True)
        print(f"evidence written to {self._results_dir}")
        failed = [outcome.name for outcome in outcomes if "error" in outcome.summary]
        if failed:
            print(f"scenarios with errors: {', '.join(failed)}")
            return 1
        return 0

    def _preserve_stderr(self, outcomes: list[ScenarioOutcome]) -> None:
        """Copy failed scenarios' pi stderr into the evidence dir.

        The scratch teardown below would otherwise destroy the one file
        kept precisely so a crash survives for the report.
        """
        for outcome in outcomes:
            if "error" not in outcome.summary:
                continue
            source = SCRATCH_ROOT / outcome.name / "pi_stderr.log"
            target = self._results_dir / f"{outcome.name}.pi_stderr.log"
            if not source.exists():
                target.write_text("<pi_stderr.log was never created>\n", "utf-8")
                continue
            text = source.read_text(encoding="utf-8", errors="replace")
            target.write_text(self._sanitizer.scrub(text), encoding="utf-8")

    def _guarded(
        self, name: str, scenario: Callable[[PiRpcSession], dict[str, object]]
    ) -> ScenarioOutcome:
        """One scenario against one session; the transcript ALWAYS survives.

        The guard owns the session so a failing scenario still yields its
        real transcript — a miss at analysis time (a marker that never
        came) must not erase the evidence of the live turn that produced
        it. The child's exit code is recorded either way.
        """
        print(f"--- scenario: {name}")
        summary: dict[str, object]
        # None until spawn succeeds: a spawn/seed failure must be THIS
        # scenario's error outcome, not a run abort with no evidence.
        session: PiRpcSession | None = None
        try:
            try:
                session = self._session(name)
                summary = scenario(session)
            finally:
                if session is not None:
                    session.close()
            print(f"    ok: {json.dumps(summary, sort_keys=True)[:160]}")
        except _SCENARIO_FAULTS as exc:
            # A characterization run must complete and report; the miss IS
            # the finding for that scenario.
            print(f"    error: {exc}")
            summary = {"error": str(exc)}
        # None: the child never existed, so there is no exit code to report.
        summary["pi_exit_code"] = session.exit_code() if session is not None else None
        transcript = session.transcript if session is not None else Transcript()
        return ScenarioOutcome(name=name, summary=summary, transcript=transcript)

    def _session(self, name: str) -> PiRpcSession:
        project = SCRATCH_ROOT / name
        self._seed(project)
        env = dict(os.environ)
        env["PATH"] = self._stubs.path_env(env["PATH"])
        return PiRpcSession.spawn(
            self._spec.to_argv(),
            cwd=project,
            stderr_path=project / "pi_stderr.log",
            env=env,
        )

    def _seed(self, project: Path) -> None:
        project.mkdir(parents=True, exist_ok=True)
        for index in range(1, _SEEDED_FILES + 1):
            body = "\n".join(
                f"notes_{index:02d} line {line:02d}: the {line}th observation "
                f"about subsystem {index} and its failure modes."
                for line in range(1, 41)
            )
            (project / f"notes_{index:02d}.txt").write_text(
                body + "\n", encoding="utf-8"
            )

    def _midturn_steer(self, session: PiRpcSession) -> dict[str, object]:
        session.send(RpcCommand.prompt(_LONG_TASK))
        session.wait_for(
            lambda e: e.type == "tool_execution_start",
            timeout_s=_TOOL_WAIT_S,
            description="first tool_execution_start",
        )
        steer_ns = session.send(RpcCommand.steer(self._steer_text))
        session.wait_for(
            lambda e: e.is_response_to("steer"),
            timeout_s=_ACK_WAIT_S,
            description="steer ack",
        )
        session.wait_for(
            lambda e: e.type == "agent_end",
            timeout_s=_TURN_WAIT_S,
            description="agent_end after steer",
        )
        session.settle(quiet_s=2)
        session.close()
        return self._midturn_summary(session.transcript, steer_ns)

    def _midturn_summary(
        self, transcript: Transcript, steer_ns: int
    ) -> dict[str, object]:
        analysis = TranscriptAnalysis(transcript)
        summary: dict[str, object] = {"steer_sent": True}
        ack = analysis.first_event_after(
            steer_ns, lambda e: e.is_response_to("steer"), description="steer ack"
        )
        summary["steer_to_ack_ms"] = analysis.elapsed_ms(steer_ns, ack.recv_ns)
        queue = analysis.first_event_after(
            steer_ns, lambda e: e.type == "queue_update", description="queue_update"
        )
        summary["steer_to_queue_update_ms"] = analysis.elapsed_ms(
            steer_ns, queue.recv_ns
        )
        summary["queue_update"] = queue.data
        marker = analysis.first_event_after(
            steer_ns,
            _emits("STEERED-ACK"),
            description=_assistant_marker("STEERED-ACK"),
        )
        summary["steer_to_assistant_marker_ms"] = analysis.elapsed_ms(
            steer_ns, marker.recv_ns
        )
        summary["in_flight_tool"] = self._in_flight_tool(analysis, steer_ns)
        summary["tool_starts_after_steer"] = self._count_after(
            analysis, steer_ns, "tool_execution_start"
        )
        summary["timeline"] = analysis.timeline()
        return summary

    def _in_flight_tool(
        self, analysis: TranscriptAnalysis, steer_ns: int
    ) -> dict[str, object]:
        start = analysis.first_event_after(
            0, lambda e: e.type == "tool_execution_start", description="tool start"
        )
        call_id = start.data.get("toolCallId")
        if not isinstance(call_id, str) or not call_id:
            # Without the id the end-predicate would match ANY id-less
            # tool_execution_end and fabricate a completion + latency.
            msg = "tool_execution_start carried no toolCallId; schema changed"
            raise LookupError(msg)
        try:
            end = analysis.first_event_after(
                steer_ns,
                lambda e: (
                    e.type == "tool_execution_end"
                    and e.data.get("toolCallId") == call_id
                ),
                description="in-flight tool end after steer",
            )
        except LookupError:
            return {"tool_call_id": call_id, "completed_after_steer": False}
        return {
            "tool_call_id": call_id,
            "completed_after_steer": True,
            "steer_to_tool_end_ms": analysis.elapsed_ms(steer_ns, end.recv_ns),
        }

    @staticmethod
    def _count_after(
        analysis: TranscriptAnalysis, after_ns: int, event_type: str
    ) -> int:
        count = 0
        probe_ns = after_ns
        while True:
            try:
                event = analysis.first_event_after(
                    probe_ns, lambda e: e.type == event_type, description=event_type
                )
            except LookupError:
                return count
            count += 1
            probe_ns = event.recv_ns

    def _idle_steer(self, session: PiRpcSession) -> dict[str, object]:
        summary: dict[str, object] = {}
        steer_ns = session.send(RpcCommand.steer(_IDLE_STEER_TEXT))
        session.wait_for(
            lambda e: e.is_response_to("steer"),
            timeout_s=_ACK_WAIT_S,
            description="idle steer ack",
        )
        # The two waits are separate on purpose: the started-flag must
        # never be rewritten by a LATER timeout, and a fresh prompt is
        # sent only into a session that verifiably never started a turn.
        started = self._idle_turn_started(session)
        summary["idle_steer_started_a_turn"] = started
        if started:
            try:
                session.wait_for(
                    lambda e: e.type == "agent_end",
                    timeout_s=_TURN_WAIT_S,
                    description="agent_end after idle steer",
                )
            except TimeoutError as exc:
                # Started, then hung: report exactly that — the flag
                # stands, the miss is recorded, and no prompt is sent
                # into the still-running turn.
                summary["error"] = f"idle steer started a turn that hung: {exc}"
        else:
            session.send(RpcCommand.prompt(_PROMPT_AFTER_IDLE))
            session.wait_for(
                lambda e: e.type == "agent_end",
                timeout_s=_TURN_WAIT_S,
                description="agent_end after post-idle prompt",
            )
        session.settle(quiet_s=2)
        session.close()
        analysis = TranscriptAnalysis(session.transcript)
        for marker in ("IDLESTEER-ACK", "PROMPT-ACK"):
            try:
                event = analysis.first_event_after(
                    0,
                    _emits(marker),
                    description=_assistant_marker(marker),
                )
                summary[f"steer_to_{marker}_ms"] = analysis.elapsed_ms(
                    steer_ns, event.recv_ns
                )
            except LookupError:
                # Absence of a marker is a finding, not a fault.
                summary[f"steer_to_{marker}_ms"] = None
        summary["timeline"] = analysis.timeline()
        return summary

    @staticmethod
    def _idle_turn_started(session: PiRpcSession) -> bool:
        try:
            session.wait_for(
                lambda e: e.type == "agent_start",
                timeout_s=_IDLE_PROBE_S,
                description="agent_start from idle steer",
            )
        except TimeoutError:
            return False
        return True

    def _followup_contrast(self, session: PiRpcSession) -> dict[str, object]:
        session.send(RpcCommand.prompt(_LONG_TASK))
        session.wait_for(
            lambda e: e.type == "tool_execution_start",
            timeout_s=_TOOL_WAIT_S,
            description="first tool_execution_start",
        )
        followup_ns = session.send(RpcCommand.follow_up(_FOLLOWUP_TEXT))
        session.wait_for(
            lambda e: e.is_response_to("follow_up"),
            timeout_s=_ACK_WAIT_S,
            description="follow_up ack",
        )
        session.wait_for(
            _emits("FOLLOWUP-ACK"),
            timeout_s=_TURN_WAIT_S,
            description=_assistant_marker("FOLLOWUP-ACK"),
        )
        session.settle(quiet_s=2)
        session.close()
        analysis = TranscriptAnalysis(session.transcript)
        marker = analysis.first_event_after(
            followup_ns,
            _emits("FOLLOWUP-ACK"),
            description=_assistant_marker("FOLLOWUP-ACK"),
        )
        queue = analysis.first_event_after(
            followup_ns, lambda e: e.type == "queue_update", description="queue_update"
        )
        summary: dict[str, object] = {
            "followup_to_queue_update_ms": analysis.elapsed_ms(
                followup_ns, queue.recv_ns
            ),
            "queue_update": queue.data,
            "followup_to_assistant_marker_ms": analysis.elapsed_ms(
                followup_ns, marker.recv_ns
            ),
            "agent_end_count": self._count_after(analysis, 0, "agent_end"),
            "agent_start_count": self._count_after(analysis, 0, "agent_start"),
            "timeline": analysis.timeline(),
        }
        return summary

    def _environment(self) -> dict[str, object]:
        return {
            "pi_version": _probe([str(self._spec.binary), "--version"]),
            "provider": self._spec.provider,
            "model": self._spec.model,
            "tools": list(self._spec.tools),
            "argv": self._spec.to_argv(),
            "steer_text": self._steer_text,
        }


def main() -> None:
    """CLI entry: run the three scenarios, write evidence, exit by outcome."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--steer-style",
        choices=sorted(_STEER_TEXTS),
        default="plain",
        help="phrasing of the mid-turn steer (evidence lands in results/arm1-<style>/)",
    )
    args = parser.parse_args()
    raise SystemExit(Arm1Runner(args.steer_style).run())


if __name__ == "__main__":
    main()
