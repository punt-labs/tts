# /// script
# requires-python = ">=3.13"
# dependencies = ["websockets>=14"]
# ///
"""Offline rehearsal: the whole analysis pipeline, no fork, no billing.

Two legs, both mandatory before the live capture run:

1. **Synthetic-ledger leg** -- fabricates a realistic session ledger (a
   prompt, tool cycles, a failing test run, a fix, a green run, plus a
   simulated dead-store window with lost relay sequences) and drives every
   analyzer over it: field inventory, latency, gap detection, tail
   reconstruction at four timepoints, seed build + seed-only
   reconstruction. Asserts the load-bearing outcomes.

2. **Wire leg** -- starts the real store subprocess on loopback, renders
   the real relay script, and pushes one synthetic hook payload through
   ``relay.sh -> relay_stamp.py -> mcp-proxy -> store``. Proves the exact
   command line the fork's settings will run works end to end before any
   claude session exists.

Run:  uv run dry_run.py
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import final

from field_inventory import FieldInventory
from gap_check import GapReport
from latency import LatencyReport
from reconstructor import TailReconstructor
from seed_builder import SEED_BUDGET_BYTES, SeedBuilder, SeedReconstructor
from stamp import HookLedger, HookRecord, SequenceStamper
from wiring import RelayScript

_SPIKE_DIR = Path(__file__).parent
_TMP = _SPIKE_DIR / ".tmp" / "dry_run"

_SESSION = "dryrun-session-0001"
_FAIL_OUTPUT = (
    "test_counts_whitespace_separated_words ... FAIL\n"
    "AssertionError: 1 != 4\n"
    "Ran 4 tests in 0.002s\n\nFAILED (failures=1)"
)
_OK_OUTPUT = "Ran 4 tests in 0.002s\n\nOK"


@final
class SyntheticSession:
    """Fabricates a plausible ledger for one working session."""

    __slots__ = ("_relay_seq", "_stamper")

    _relay_seq: int
    _stamper: SequenceStamper

    def __new__(cls) -> SyntheticSession:
        self = super().__new__(cls)
        self._stamper = SequenceStamper()
        self._relay_seq = 0
        return self

    def _stamped(
        self, event: str, extra: dict[str, object], *, lost: bool = False
    ) -> HookRecord | None:
        # Sender side always advances; a lost event consumes a relay_seq
        # but never reaches the stamper -- exactly the dead-store window.
        self._relay_seq += 1
        if lost:
            return None
        payload: dict[str, object] = {
            "session_id": _SESSION,
            "hook_event_name": event,
            "transcript_path": f"~/.claude/projects/x/{_SESSION}.jsonl",
            "cwd": "<scratch>/project",
            "relay_seq": self._relay_seq,
            "relay_start_ns": time.time_ns() - 25_000_000,
            **extra,
        }
        return self._stamper.stamp(event, payload)

    def build(self, ledger: HookLedger) -> None:
        """Write the synthetic session into the ledger."""
        prompt = "Fix the failing textstat suite, then add readability."
        events: list[tuple[str, dict[str, object], bool]] = [
            ("SessionStart", {"source": "startup"}, False),
            ("UserPromptSubmit", {"prompt": prompt}, False),
            self._tool("Bash", {"command": "python3 -m unittest"}, _FAIL_OUTPUT),
            self._tool("Read", {"file_path": "<scratch>/project/textstat/stats.py"}),
            self._tool("Edit", {"file_path": "<scratch>/project/textstat/stats.py"}),
            self._tool("Bash", {"command": "python3 -m unittest"}, _OK_OUTPUT),
            # Dead-store window: four events lost.
            ("PreToolUse", {"tool_name": "Write"}, True),
            self._lost_tool("Write", "<scratch>/project/textstat/readability.py"),
            ("PreToolUse", {"tool_name": "Bash"}, True),
            (
                "PostToolUse",
                {"tool_name": "Bash", "tool_input": {"command": "ls"}},
                True,
            ),
            self._tool("Bash", {"command": "python3 -m unittest -v"}, _OK_OUTPUT),
            ("Stop", {"stop_hook_active": False}, False),
        ]
        for event, extra, lost in events:
            record = self._stamped(event, extra, lost=lost)
            if record is not None:
                ledger.append(record)

    @staticmethod
    def _tool(
        name: str, tool_input: dict[str, object], response: str = "done"
    ) -> tuple[str, dict[str, object], bool]:
        return (
            "PostToolUse",
            {"tool_name": name, "tool_input": tool_input, "tool_response": response},
            False,
        )

    @staticmethod
    def _lost_tool(name: str, path: str) -> tuple[str, dict[str, object], bool]:
        return (
            "PostToolUse",
            {"tool_name": name, "tool_input": {"file_path": path}},
            True,
        )


def _check(*, condition: bool, label: str, failures: list[str]) -> None:
    status = "ok" if condition else "FAIL"
    print(f"  [{status}] {label}")
    if not condition:
        failures.append(label)


def _session_gap_body(records: tuple[HookRecord, ...]) -> dict[str, object]:
    sessions = GapReport(records).as_dict()["sessions"]
    if not isinstance(sessions, dict):
        msg = "gap report has no sessions object"
        raise TypeError(msg)
    body = sessions[_SESSION]
    if not isinstance(body, dict):
        msg = f"gap report body for {_SESSION} is not an object"
        raise TypeError(msg)
    return body


def _synthetic_leg(failures: list[str]) -> None:
    print("[1] synthetic-ledger leg")
    ledger_path = _TMP / "synthetic_ledger.jsonl"
    ledger_path.unlink(missing_ok=True)
    ledger = HookLedger(ledger_path)
    SyntheticSession().build(ledger)
    records = ledger.records()

    inventory = FieldInventory(records).as_dict()
    _check(
        condition=isinstance(inventory.get("PostToolUse"), dict),
        label="inventory profiles PostToolUse",
        failures=failures,
    )
    overall = LatencyReport(records).as_dict()["overall"]
    _check(
        condition=isinstance(overall, dict) and overall["n"] == len(records),
        label="latency covers every stamped record",
        failures=failures,
    )
    gap_body = _session_gap_body(records)
    _check(
        condition=gap_body["gap_detected"] is True,
        label="gap detected",
        failures=failures,
    )
    _check(
        condition=gap_body["lost_events"] == 4,
        label="loss quantified (4 events)",
        failures=failures,
    )

    cutoffs = {"early": 3, "mid-debug": 3, "post-fix": 6, "end": len(records)}
    for label, cutoff in cutoffs.items():
        print(TailReconstructor(records, cutoff).answer(label).render())
        print()
    mid = TailReconstructor(records, 3).answer("mid-debug")
    _check(
        condition="FAILED" in mid.open_failure,
        label="mid-debug reconstructs failure",
        failures=failures,
    )
    end = TailReconstructor(records, len(records)).answer("end")
    _check(
        condition=end.open_failure == "",
        label="end shows no open failure",
        failures=failures,
    )

    seed = SeedBuilder(records, len(records)).build()
    _check(
        condition=seed.byte_size() <= SEED_BUDGET_BYTES,
        label="seed within budget",
        failures=failures,
    )
    seed_answer = SeedReconstructor(seed).answer("end")
    print(seed_answer.render())
    _check(
        condition=seed_answer.goal == end.goal,
        label="seed retains the goal",
        failures=failures,
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wire_leg(failures: list[str]) -> None:
    print("[2] wire leg: relay.sh -> relay_stamp.py -> mcp-proxy -> store")
    proxy = shutil.which("mcp-proxy")
    if proxy is None:
        _check(condition=False, label="mcp-proxy on PATH", failures=failures)
        return
    port = _free_port()
    ledger_path = _TMP / "wire_ledger.jsonl"
    ledger_path.unlink(missing_ok=True)
    counter_dir = _TMP / "counters"
    shutil.rmtree(counter_dir, ignore_errors=True)
    relay = _TMP / "relay.sh"
    body = RelayScript(
        Path(proxy),
        f"ws://127.0.0.1:{port}",
        _SPIKE_DIR / "relay_stamp.py",
        counter_dir,
    ).render()
    relay.parent.mkdir(parents=True, exist_ok=True)
    relay.write_text(body, encoding="utf-8")
    relay.chmod(0o755)

    store = subprocess.Popen(
        [
            "uv",
            "run",
            str(_SPIKE_DIR / "hook_store.py"),
            "--port",
            str(port),
            "--ledger",
            str(ledger_path),
        ],
        cwd=_SPIKE_DIR,
        start_new_session=True,
    )
    try:
        _await_port(port)
        payload = json.dumps(
            {
                "session_id": "wire-test",
                "hook_event_name": "Stop",
                "cwd": "<scratch>/project",
            }
        )
        for _ in range(2):
            sent = subprocess.run(
                [str(relay), "Stop"],
                input=payload,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            _check(
                condition=sent.returncode == 0,
                label="relay pipeline exits 0",
                failures=failures,
            )
        # A stamper crash must be LOUD: POSIX sh has no pipefail, so the
        # relay runs the stamper in a command substitution and exits
        # nonzero itself. Invalid stdin crashes json.load BEFORE the
        # counter advances -- exactly the silent-drop shape that would
        # otherwise be invisible to gap detection.
        crashed = subprocess.run(
            [str(relay), "Stop"],
            input="this is not json",
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        _check(
            condition=crashed.returncode != 0,
            label="stamper crash makes the hook exit nonzero",
            failures=failures,
        )
        records = _await_records(ledger_path, expected=2)
        _check(
            condition=len(records) == 2,
            label="store persisted both relayed events (crash relayed nothing)",
            failures=failures,
        )
        if len(records) == 2:
            _check(
                condition=[r.relay_seq() for r in records] == [1, 2],
                label="sender-side relay_seq increments across commands",
                failures=failures,
            )
            start_ns = records[0].relay_start_ns() or 0
            latency_ms = (records[0].received_ns - start_ns) / 1e6
            _check(
                condition=0 < latency_ms < 10_000,
                label=f"latency sane ({latency_ms:.1f}ms)",
                failures=failures,
            )
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(store.pid), signal.SIGTERM)
        store.wait(timeout=10)


def _await_records(ledger_path: Path, expected: int) -> tuple[HookRecord, ...]:
    deadline = time.monotonic() + 10
    records: tuple[HookRecord, ...] = ()
    while time.monotonic() < deadline:
        records = HookLedger(ledger_path).records_snapshot()
        if len(records) >= expected:
            break
        time.sleep(0.3)
    return records


def _await_port(port: int) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(1)
            try:
                probe.connect(("127.0.0.1", port))
            except OSError:
                time.sleep(0.3)
            else:
                return
    msg = "store never opened its port"
    raise RuntimeError(msg)


def main() -> None:
    """Run both rehearsal legs; exit nonzero on any failed check."""
    _TMP.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    _synthetic_leg(failures)
    _wire_leg(failures)
    if failures:
        print(f"DRY RUN FAIL: {failures}")
        raise SystemExit(1)
    print("DRY RUN PASS")


if __name__ == "__main__":
    main()
