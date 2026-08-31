# /// script
# requires-python = ">=3.13"
# dependencies = ["websockets>=14"]
# ///
"""Arm 2 live run: steer a claude TUI in tmux via send-keys, receipts by hook.

One fork, five cases against the same session:

1. **idle_inject** — send-keys a prompt at a turn boundary.
2. **midturn_inject** — send while the agent is mid-task: queue or drop?
3. **esc_steer** — Escape to interrupt, then replacement text (hard steer).
4. **paste_multiline** — bracketed paste of a multi-line message, then Enter.
5. **literal_specials** — literal-mode send of flag/quote/keyname text.

The delivery receipt is the hook store: the injected text surfacing as a
``UserPromptSubmit`` record (sender-stamped relay, receipt-ns for latency);
``capture-pane`` snapshots are the secondary witness. Isolation per h7k8:
scratch project + fresh CLAUDE_CONFIG_DIR outside the repo, sentinel
vox/vox-panel stubs first on PATH, no Bash in the profile, verified
teardown. Run from this directory:

    direnv exec ../../ uv run run_arm2.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self, final

from websockets.asyncio.client import connect

from launcher import SESSION_PREFIX, SessionLauncher, TmuxSession
from ledger_watch import LedgerWatch, prompt_of
from scratch import IsolatedConfig, ScratchProject, SeededFile
from stamp import HookLedger, HookRecord, Sanitizer
from stubs import SentinelStubs
from teardown import Teardown
from wiring import STEER_INJECT_V1, HookWiring, RelayScript, SettingsDocument

_SPIKE_DIR = Path(__file__).parent
# Outside the repo (h7k8): the fork's cwd and config dir must live in an
# unenabled directory the repo's own gates never see.
_SCRATCH_ROOT = Path.home() / ".cache" / "vox04qy-scratch"
_RESULTS = _SPIKE_DIR / "results" / "arm2"

_SESSION_START_WAIT_S = 240
_TURN_WAIT_S = 420
_RECEIPT_WAIT_S = 300
_POLL_S = 1.0
_PASTE_SETTLE_S = 1.0

# Interactive-UI chrome the fork may show before working (73y7's list):
# each is answered once, keystrokes logged.
_DIALOG_MARKERS: dict[str, tuple[str, ...]] = {
    "Yes, I trust this folder": ("Down", "Enter"),
    "Choose the text style": ("Enter",),
    "Press Enter to": ("Enter",),
    "Detected a custom API key": ("Enter",),
}
_LOGIN_BANNER_MARKER = "Your login expires"
_LOGIN_BANNER_PLACEHOLDER = "<login-status banner removed>"

_INITIAL_PROMPT = (
    "Read notes_01.txt with the Read tool and reply with only its first "
    "line. Then wait for further instructions."
)
_LONG_TASK = (
    "Read each of the six files notes_01.txt through notes_06.txt, one at "
    "a time in order, and after each one use the Write tool to create "
    "summary_XX.md containing a two-sentence summary. Work through all six."
)
_SECOND_TASK = (
    "Read each of the six files notes_01.txt through notes_06.txt again, "
    "one at a time, and use the Write tool to create conclusion_XX.md with "
    "a one-sentence conclusion for each. Work through all six."
)
_IDLE_TEXT = "Reply with exactly IDLEACK-vox04qy and nothing else."
_MIDTURN_TEXT = (
    "Please stop after the file you are working on now and reply with "
    "exactly MIDACK-vox04qy."
)
_ESC_TEXT = "Reply with exactly ESCACK-vox04qy and nothing else."
_PASTE_TEXT = (
    "PASTEACK-vox04qy line one of a pasted block\n"
    "line two stays in the same message\n"
    "Reply with exactly PASTE-DONE."
)
_LITERAL_TEXT = (
    "LITERALACK-vox04qy specials: -l --flag 'single' \"double\" ; & | Enter C-c"
)


# Everything a live case can legitimately fail with. SubprocessError covers
# the dominant family here — every tmux send/capture is check=True — which
# OSError does NOT (CalledProcessError is not an OSError).
_CASE_FAULTS = (
    TimeoutError,
    LookupError,
    OSError,
    subprocess.SubprocessError,
    RuntimeError,
)


def _probe(argv: list[str]) -> str:
    """A provenance probe that records failure instead of blanking.

    An empty string in the committed environment block is not a recording
    of what went wrong.
    """
    result = subprocess.run(argv, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        return f"<probe failed rc={result.returncode}: {detail[:80]}>"
    return result.stdout.strip()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@final
class StoreProcess:
    """The stub voxd store as a subprocess (73y7's shape, no gap window)."""

    __slots__ = ("_ledger_path", "_port", "_process")

    _ledger_path: Path
    _port: int
    _process: subprocess.Popen[bytes] | None

    def __new__(cls, port: int, ledger_path: Path) -> Self:
        self = super().__new__(cls)
        self._port = port
        self._ledger_path = ledger_path
        self._process = None
        return self

    @property
    def url(self) -> str:
        """Loopback WebSocket URL the relays dial."""
        return f"ws://127.0.0.1:{self._port}"

    @property
    def ledger_path(self) -> Path:
        """Where the store's JSONL ledger lives."""
        return self._ledger_path

    def start(self) -> None:
        """Launch the store and wait for store/health."""
        self._process = subprocess.Popen(
            [
                "uv",
                "run",
                str(_SPIKE_DIR / "hook_store.py"),
                "--port",
                str(self._port),
                "--ledger",
                str(self._ledger_path),
                "--scratch-root",
                str(_SCRATCH_ROOT),
            ],
            cwd=_SPIKE_DIR,
            start_new_session=True,
        )
        asyncio.run(self._await_health())

    def stop_if_running(self) -> None:
        """Kill the group at cleanup time; fine if already dead."""
        if self._process is None or self._process.poll() is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
            self._process.wait(timeout=10)

    async def _await_health(self) -> None:
        deadline = time.monotonic() + 30
        frame = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "store/health"})
        while time.monotonic() < deadline:
            try:
                async with connect(self.url) as conn:
                    await conn.send(frame)
                    await asyncio.wait_for(conn.recv(), timeout=5)
                    return
            except (TimeoutError, OSError):
                await asyncio.sleep(0.5)
        msg = "hook store never became healthy"
        raise RuntimeError(msg)


def _seeded_notes() -> tuple[SeededFile, ...]:
    files = []
    for index in range(1, 7):
        body = "\n".join(
            f"notes_{index:02d} line {line:02d}: observation {line} about "
            f"subsystem {index}."
            for line in range(1, 31)
        )
        files.append(SeededFile(f"notes_{index:02d}.txt", body + "\n"))
    return tuple(files)


@final
@dataclass(frozen=True, slots=True)
class CaseResult:
    """One matrix case's derived summary."""

    name: str
    summary: dict[str, object]


@final
class Arm2Runner:
    """One fork, five injection cases, hook-store receipts."""

    __slots__ = (
        "_nudges",
        "_sanitizer",
        "_session",
        "_stubs",
        "_watch",
    )

    _nudges: list[str]
    _sanitizer: Sanitizer
    _session: TmuxSession | None
    _stubs: SentinelStubs
    _watch: LedgerWatch | None

    def __new__(cls) -> Self:
        for binary in ("claude", "tmux", "mcp-proxy"):
            if shutil.which(binary) is None:
                msg = f"{binary} is not on PATH"
                raise RuntimeError(msg)
        self = super().__new__(cls)
        self._stubs = SentinelStubs(_SCRATCH_ROOT / "stubs")
        self._sanitizer = Sanitizer.for_host(_SCRATCH_ROOT)
        self._session = None
        self._watch = None
        self._nudges = []
        return self

    @property
    def stubs(self) -> SentinelStubs:
        """The sentinel stand-ins guarding the vox surface."""
        return self._stubs

    def run(self) -> int:
        """Fork, run the matrix, write evidence, tear down; exit code."""
        ledger_path = _RESULTS / "hook_ledger.jsonl"
        if ledger_path.exists():
            # The store APPENDS; a rerun over an old ledger makes every
            # receipt wait match the previous run's records (observed
            # live: negative latencies, stale recv_seqs). Refuse before
            # anything spawns; the operator moves the old evidence aside.
            msg = f"ledger already exists: {ledger_path} — move the old run aside first"
            raise RuntimeError(msg)
        _RESULTS.mkdir(parents=True, exist_ok=True)
        self._stubs.create()
        store = StoreProcess(_free_port(), ledger_path)
        results: list[CaseResult] = []
        try:
            store.start()
            print(f"store healthy at {store.url}")
            self._launch(store)
            watch = self._watch
            if watch is None:  # pragma: no cover - _launch always sets it
                msg = "watch not initialized"
                raise RuntimeError(msg)
            watch.wait_for(
                lambda r: r.event == "SessionStart",
                timeout_s=_SESSION_START_WAIT_S,
                description="SessionStart",
            )
            print("SessionStart received; awaiting initial turn Stop")
            self._await_stops(1)
            for name, case in (
                ("idle_inject", self._idle_inject),
                ("midturn_inject", self._midturn_inject),
                ("esc_steer", self._esc_steer),
                ("paste_multiline", self._paste_multiline),
                ("literal_specials", self._literal_specials),
            ):
                print(f"--- case: {name}")
                try:
                    results.append(case())
                except _CASE_FAULTS as exc:
                    # The matrix must complete; the miss IS the finding.
                    print(f"    error: {exc}")
                    results.append(CaseResult(name, {"error": str(exc)}))
        finally:
            stub_lines, teardown_lines, teardown_clean = self.teardown_with_evidence(
                store
            )
        self._write_summary(
            results, stub_lines, teardown_lines, teardown_clean=teardown_clean
        )
        failed = [result.name for result in results if "error" in result.summary]
        if failed:
            print(f"cases with errors: {', '.join(failed)}")
            return 1
        if not teardown_clean:
            # A run that leaves anything on disk — a credentials copy in
            # the worst case — must not report success.
            print("teardown left residue on disk; run FAILS")
            return 1
        return 0

    def _launch(self, store: StoreProcess) -> None:
        session_name = f"{SESSION_PREFIX}-{datetime.now(tz=UTC):%H%M%S}"
        project = ScratchProject(_SCRATCH_ROOT / session_name / "project")
        config = IsolatedConfig(_SCRATCH_ROOT / session_name / "claude-config")
        proxy = Path(shutil.which("mcp-proxy") or "")
        relay_body = RelayScript(
            proxy, store.url, config.stamper_script, config.counter_dir
        ).render()
        settings = SettingsDocument(STEER_INJECT_V1, HookWiring(config.relay_script))
        project.create(settings.render(), _seeded_notes())
        config.create(project.path, Path.home() / ".claude" / ".credentials.json")
        config.deposit_relay(relay_body)
        if not config.credentials_seeded:
            print("WARNING: no file-based credentials; fork will demand /login")
        claude = Path(shutil.which("claude") or "claude")
        self._session = SessionLauncher(claude).launch(
            session_name,
            project,
            config,
            _INITIAL_PROMPT,
            extra_env={"PATH": self._stubs.path_env(os.environ["PATH"])},
        )
        print(f"tmux session: {session_name}")
        self._watch = LedgerWatch(
            HookLedger(store.ledger_path), poll_s=_POLL_S, on_tick=self._nudge_dialogs
        )

    def _idle_inject(self) -> CaseResult:
        session, watch = self._live()
        sent_ns = time.time_ns()
        session.send_line(_IDLE_TEXT)
        record = watch.wait_for(
            lambda r: r.event == "UserPromptSubmit" and "IDLEACK" in prompt_of(r),
            timeout_s=_RECEIPT_WAIT_S,
            description="idle UserPromptSubmit",
        )
        self._capture("idle_inject")
        summary = self._receipt_summary(sent_ns, record)
        self._await_stops(2)
        return CaseResult("idle_inject", summary)

    def _midturn_inject(self) -> CaseResult:
        session, watch = self._live()
        session.send_line(_LONG_TASK)
        task_record = watch.wait_for(
            lambda r: r.event == "UserPromptSubmit" and "summary_XX" in prompt_of(r),
            timeout_s=_RECEIPT_WAIT_S,
            description="long-task UserPromptSubmit",
        )
        watch.wait_for(
            lambda r: (
                r.event == "PostToolUse" and r.received_ns > task_record.received_ns
            ),
            timeout_s=_TURN_WAIT_S,
            description="first PostToolUse of the long task",
        )
        stops_at_send = watch.count("Stop")
        sent_ns = time.time_ns()
        session.send_line(_MIDTURN_TEXT)
        time.sleep(1.5)
        self._capture("midturn_sent")
        record = watch.wait_for(
            lambda r: r.event == "UserPromptSubmit" and "MIDACK" in prompt_of(r),
            timeout_s=_RECEIPT_WAIT_S,
            description="mid-turn UserPromptSubmit",
        )
        self._capture("midturn_received")
        summary = self._receipt_summary(sent_ns, record)
        summary["stops_at_send"] = stops_at_send
        summary["stops_before_delivery"] = self._stops_before(record.received_ns)
        summary["tool_results_between_send_and_delivery"] = sum(
            1
            for r in watch.records()
            if r.event == "PostToolUse" and sent_ns < r.received_ns < record.received_ns
        )
        self._await_stops(3)
        return CaseResult("midturn_inject", summary)

    def _esc_steer(self) -> CaseResult:
        session, watch = self._live()
        session.send_line(_SECOND_TASK)
        task_record = watch.wait_for(
            lambda r: r.event == "UserPromptSubmit" and "conclusion_XX" in prompt_of(r),
            timeout_s=_RECEIPT_WAIT_S,
            description="second-task UserPromptSubmit",
        )
        watch.wait_for(
            lambda r: (
                r.event == "PostToolUse" and r.received_ns > task_record.received_ns
            ),
            timeout_s=_TURN_WAIT_S,
            description="first PostToolUse of the second task",
        )
        stops_before_esc = watch.count("Stop")
        session.send_key("Escape")
        time.sleep(1.5)
        self._capture("esc_pressed")
        sent_ns = time.time_ns()
        session.send_line(_ESC_TEXT)
        record = watch.wait_for(
            lambda r: r.event == "UserPromptSubmit" and "ESCACK" in prompt_of(r),
            timeout_s=_RECEIPT_WAIT_S,
            description="post-Escape UserPromptSubmit",
        )
        self._capture("esc_received")
        summary = self._receipt_summary(sent_ns, record)
        summary["stops_before_escape"] = stops_before_esc
        summary["stops_before_delivery"] = self._stops_before(record.received_ns)
        self._await_next_stop()
        return CaseResult("esc_steer", summary)

    def _paste_multiline(self) -> CaseResult:
        session, watch = self._live()
        sent_ns = time.time_ns()
        session.paste_text(_PASTE_TEXT)
        time.sleep(_PASTE_SETTLE_S)
        session.send_key("Enter")
        record = watch.wait_for(
            lambda r: r.event == "UserPromptSubmit" and "PASTEACK" in prompt_of(r),
            timeout_s=_RECEIPT_WAIT_S,
            description="pasted UserPromptSubmit",
        )
        self._capture("paste_multiline")
        summary = self._receipt_summary(sent_ns, record)
        prompt = prompt_of(record)
        summary["all_three_lines_in_one_prompt"] = (
            "line one" in prompt and "line two" in prompt and "PASTE-DONE" in prompt
        )
        self._await_next_stop()
        return CaseResult("paste_multiline", summary)

    def _literal_specials(self) -> CaseResult:
        session, watch = self._live()
        sent_ns = time.time_ns()
        session.send_literal(_LITERAL_TEXT)
        time.sleep(0.5)
        session.send_key("Enter")
        record = watch.wait_for(
            lambda r: r.event == "UserPromptSubmit" and "LITERALACK" in prompt_of(r),
            timeout_s=_RECEIPT_WAIT_S,
            description="literal UserPromptSubmit",
        )
        self._capture("literal_specials")
        summary = self._receipt_summary(sent_ns, record)
        summary["text_arrived_verbatim"] = prompt_of(record) == _LITERAL_TEXT
        summary["prompt_as_received"] = prompt_of(record)
        self._await_next_stop()
        return CaseResult("literal_specials", summary)

    def _receipt_summary(self, sent_ns: int, record: HookRecord) -> dict[str, object]:
        summary: dict[str, object] = {
            "send_to_hook_visible_ms": (record.received_ns - sent_ns) / 1e6,
            "receipt_event": record.event,
            "receipt_recv_seq": record.recv_seq,
            "receipt_relay_seq": record.relay_seq(),
        }
        relay_start = record.relay_start_ns()
        if relay_start is not None:
            summary["hook_fire_to_store_ms"] = (record.received_ns - relay_start) / 1e6
        return summary

    def _stops_before(self, cutoff_ns: int) -> int:
        watch = self._watch
        if watch is None:  # pragma: no cover - set at launch
            return 0
        return sum(
            1
            for r in watch.records()
            if r.event == "Stop" and r.received_ns < cutoff_ns
        )

    def _await_stops(self, count: int) -> None:
        _session, watch = self._live()
        watch.wait_until(
            lambda: watch.count("Stop") >= count,
            timeout_s=_TURN_WAIT_S,
            description=f"Stop count >= {count}",
        )

    def _await_next_stop(self) -> None:
        _session, watch = self._live()
        self._await_stops(watch.count("Stop") + 1)

    def _live(self) -> tuple[TmuxSession, LedgerWatch]:
        if self._session is None or self._watch is None:
            msg = "session not launched"
            raise RuntimeError(msg)
        return self._session, self._watch

    def _nudge_dialogs(self) -> None:
        session = self._session
        if session is None:
            return
        if not session.alive():
            self._note_once(f"session {session.name} is DEAD")
            return
        try:
            pane = session.capture()
        except subprocess.CalledProcessError:
            # The pane vanished between the alive check and the capture;
            # say so once instead of surfacing only as a receipt timeout.
            self._note_once(f"session {session.name} pane vanished mid-poll")
            return
        for marker, keys in _DIALOG_MARKERS.items():
            note = f"nudged dialog: {marker!r} with {keys}"
            if marker in pane and note not in self._nudges:
                for key in keys:
                    session.send_key(key)
                    time.sleep(0.5)
                self._nudges.append(note)
                print(f"    {note}")
                return

    def _capture(self, label: str) -> None:
        target = _RESULTS / f"pane_{label}.txt"
        session = self._session
        if session is None or not session.alive():
            # The secondary witness is MISSING — write that down instead
            # of silently omitting the file.
            self._note_once(f"pane capture {label!r} unavailable: session dead")
            target.write_text(
                "<pane capture unavailable: session dead>\n", encoding="utf-8"
            )
            return
        pane = session.capture()
        cleaned = "\n".join(
            _LOGIN_BANNER_PLACEHOLDER if _LOGIN_BANNER_MARKER in line else line
            for line in pane.splitlines()
        )
        target.write_text(self._sanitizer.scrub(cleaned) + "\n", encoding="utf-8")

    def _note_once(self, note: str) -> None:
        # Rides the dialog-nudge channel into the summary, deduplicated:
        # the poll loop would otherwise repeat it every tick.
        if note not in self._nudges:
            self._nudges.append(note)
            print(f"    {note}")

    def teardown_with_evidence(
        self, store: StoreProcess
    ) -> tuple[list[str], list[str], bool]:
        """Harvest the stub evidence, THEN destroy everything.

        The invocation log lives under the scratch root the teardown
        removes; reading it after the rmtree would fabricate a "zero
        hits" all-clear (the first live run did exactly that). Returns
        (stub invocation lines, teardown log lines, both-passes-clean).
        """
        stub_lines = list(self._stubs.invocations())
        lines: list[str] = []
        session = self._session
        if session is not None:
            session.kill()
            # Wait for the pane process to actually die, then let its
            # exit flushes land: a dying claude recreates its (empty)
            # CLAUDE_CONFIG_DIR skeleton at exit, and an rmtree racing
            # that flush reports clean while leaving the skeleton behind
            # — observed on the first live run.
            deadline = time.monotonic() + 10
            while session.alive() and time.monotonic() < deadline:
                time.sleep(0.5)
            time.sleep(1.0)
            lines.append(f"killed tmux session {session.name}")
        store.stop_if_running()
        lines.append("store stopped")
        clean = True
        for attempt in (1, 2):
            outcome = Teardown(_SCRATCH_ROOT).run()
            clean = clean and outcome.clean
            lines.append(f"teardown pass {attempt} clean={outcome.clean}")
            lines.extend(outcome.log)
        return stub_lines, lines, clean

    def _write_summary(
        self,
        results: list[CaseResult],
        stub_lines: list[str],
        teardown_lines: list[str],
        *,
        teardown_clean: bool,
    ) -> None:
        body = {
            "environment": self._environment(),
            "cases": {result.name: result.summary for result in results},
            "dialog_nudges": self._nudges,
            "stub_invocations": stub_lines,
            "teardown": teardown_lines,
            "teardown_clean": teardown_clean,
        }
        (_RESULTS / "summary.json").write_text(
            self._sanitizer.scrub(json.dumps(body, indent=2, sort_keys=True)) + "\n",
            encoding="utf-8",
        )
        (_RESULTS / "teardown.log").write_text(
            self._sanitizer.scrub("\n".join(teardown_lines)) + "\n", encoding="utf-8"
        )
        print(f"evidence written to {_RESULTS}")

    def _environment(self) -> dict[str, object]:
        return {
            "claude_version": _probe(["claude", "--version"]),
            "tmux_version": _probe(["tmux", "-V"]),
            "profile": STEER_INJECT_V1.name,
        }


def main() -> None:
    """CLI entry: run the matrix, write evidence, exit by outcome."""
    raise SystemExit(Arm2Runner().run())


if __name__ == "__main__":
    main()
