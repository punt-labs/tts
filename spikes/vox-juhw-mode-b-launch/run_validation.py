# /// script
# requires-python = ">=3.13"
# dependencies = ["websockets>=14"]
# ///
"""End-to-end Mode B v1 validation: fork -> configure -> attach -> loopback.

One run of this script produces the mission's four evidence items under
``results/run_<ts>/``:

1. ``hook_ledger.jsonl`` -- hooks from the spawned session, stamped and
   attributed by the stub voxd store.
2. ``capture_mid_run.txt`` -- the pane contents while the fork works (the
   non-interactive ``tmux attach`` proof).
3. ``capture_after_store_kill.txt`` + ``capture_post_kill_turn.txt`` +
   ``survival.log`` -- the fork outlives a SIGKILL of the store and still
   answers a follow-up turn.
4. ``teardown.log`` -- two teardown passes, both clean.

Plus ``verdict.json``: the per-criterion and overall PASS/FAIL.

Run from this directory:  uv run run_validation.py
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
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Self, final

from websockets.asyncio.client import connect

from launcher import SESSION_PREFIX, SessionLauncher, TmuxSession
from profiles import VOICE_LAUNCH_V1, HookWiring, SettingsDocument
from scratch import IsolatedConfig, ScratchProject
from stamp import HookLedger
from teardown import Teardown
from transcript import TaskSeed

_SPIKE_DIR = Path(__file__).parent
_SCRATCH_ROOT = _SPIKE_DIR / ".tmp"

# Interactive-UI chrome the fork may show before working; the poller answers
# each once, with the keystrokes listed, and logs the nudge as a rough edge.
# The workspace-trust dialog appears despite the pre-seeded
# hasTrustDialogAccepted because the deposited settings.json pre-approves
# tools, which triggers a stronger variant whose default is "No, exit" --
# hence Down + Enter to select "Yes, I trust this folder".
_DIALOG_MARKERS: dict[str, tuple[str, ...]] = {
    "Yes, I trust this folder": ("Down", "Enter"),
    "Choose the text style": ("Enter",),
    "Press Enter to": ("Enter",),
    # Fallback only: the fork's env blanks ANTHROPIC_API_KEY, but if a key
    # still leaks through, accept the recommended "No" default.
    "Detected a custom API key": ("Enter",),
}

# How long the run waits for the spawned session's hooks to arrive.
_HOOK_WAIT_S = 240
_POLL_INTERVAL_S = 3.0

# The hook events a complete run MUST deliver; a ledger that is ordered and
# attributed but missing any of these is an incomplete run and fails the
# hooks criterion outright.
_REQUIRED_EVENTS = frozenset({"SessionStart", "UserPromptSubmit", "Stop"})

# The mid-run pane must show evidence of the seeded task -- the derived
# prompt names this file and the fork's first tool call writes it. A merely
# non-empty pane (stalled UI, error screen) is not a usable session.
_PANE_TASK_MARKER = "greeting.py"

# Post-kill liveness probe. The typed prompt ECHOES into the pane the moment
# send-keys lands, so the expected reply must be a string the prompt itself
# never contains -- the fork has to assemble it. Matching a marker that
# appears verbatim in the sent text would go true on the echo alone, even
# for a hung fork.
_SURVIVAL_PROMPT = (
    "Reply with the two fragments ALI and VE joined into one uppercase "
    "word, no space, and nothing else."
)
_SURVIVAL_MARKER = "ALIVE"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@final
class StoreProcess:
    """The stub voxd store as a killable subprocess."""

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
        """Launch the store and wait for it to answer store/health."""
        # start_new_session puts the `uv run` wrapper AND its python child
        # in one process group: killing only the wrapper's pid would orphan
        # the actual store and fake the survival evidence.
        self._process = subprocess.Popen(
            [
                "uv",
                "run",
                str(_SPIKE_DIR / "hook_store.py"),
                "--port",
                str(self._port),
                "--ledger",
                str(self._ledger_path),
            ],
            cwd=_SPIKE_DIR,
            start_new_session=True,
        )
        asyncio.run(self._await_health())

    def sigkill(self) -> None:
        """SIGKILL the store's whole process group -- the hard failure mode."""
        if self._process is None:
            msg = "store was never started"
            raise RuntimeError(msg)
        os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
        self._process.wait(timeout=10)

    def stop_if_running(self) -> None:
        """Kill the group at cleanup time; fine if already dead.

        Must not raise from a ``finally`` block: a stuck store escalates
        SIGTERM -> SIGKILL rather than letting ``TimeoutExpired`` propagate
        and mask the original exception.
        """
        if self._process is None or self._process.poll() is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # Even SIGKILL cannot reap a D-state process; note it and
                # return rather than raising out of the caller's finally.
                print(
                    f"WARNING: store pid {self._process.pid} unreaped after "
                    "SIGKILL (uninterruptible sleep?)",
                    file=sys.stderr,
                )

    def confirmed_dead(self) -> bool:
        """True once the loopback port refuses connections.

        Polls briefly: SIGKILL delivery and the kernel's socket teardown
        are not atomic, and a probe in that window can still connect to
        the dying listener.
        """
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self._port_refuses():
                return True
            time.sleep(0.5)
        return False

    def _port_refuses(self) -> bool:
        with socket.socket() as probe:
            probe.settimeout(2)
            try:
                probe.connect(("127.0.0.1", self._port))
            except OSError:
                return True
            return False

    async def _await_health(self) -> None:
        deadline = time.monotonic() + 30
        frame = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "store/health"})
        while time.monotonic() < deadline:
            try:
                async with connect(self.url) as conn:
                    await conn.send(frame)
                    await asyncio.wait_for(conn.recv(), timeout=5)
                    return
            except OSError:
                await asyncio.sleep(0.5)
        msg = "hook store never became healthy"
        raise RuntimeError(msg)


@final
class ValidationRun:
    """One full fork -> configure -> attach -> hook-loopback validation."""

    __slots__ = ("_notes", "_nudges", "_results_dir", "_verdicts")

    _notes: list[str]
    _nudges: list[str]
    _results_dir: Path
    _verdicts: dict[str, bool]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        stamp_dir = datetime.now(tz=UTC).strftime("run_%Y%m%d_%H%M%S")
        self._results_dir = _SPIKE_DIR / "results" / stamp_dir
        self._verdicts = {}
        self._nudges = []
        self._notes = []
        return self

    def execute(self) -> int:
        """Run the whole chain; return a process exit code."""
        self._results_dir.mkdir(parents=True, exist_ok=True)
        print(f"[0] evidence dir: {self._results_dir}")
        print("[1] pre-run teardown (idempotent)")
        Teardown(_SCRATCH_ROOT).run()

        ledger_path = self._results_dir / "hook_ledger.jsonl"
        store = StoreProcess(_free_port(), ledger_path)
        try:
            self._drive(store, ledger_path)
        finally:
            # Safety net first: the fork and the credentials copy are the
            # dangerous leftovers -- they must go even if the store stop
            # misbehaves.
            self._teardown_safety_net()
            store.stop_if_running()
        return self._write_verdict()

    def _drive(self, store: StoreProcess, ledger_path: Path) -> None:
        print("[2] starting stub voxd store")
        store.start()
        print(f"    healthy at {store.url}")

        print("[3] creating scratch project + isolated CLAUDE_CONFIG_DIR")
        session_name = f"{SESSION_PREFIX}-{datetime.now(tz=UTC):%H%M%S}"
        project = ScratchProject(_SCRATCH_ROOT / session_name / "project")
        proxy = Path(shutil.which("mcp-proxy") or "mcp-proxy")
        settings = SettingsDocument(VOICE_LAUNCH_V1, HookWiring(proxy, store.url))
        project.create(settings.render())
        config = IsolatedConfig(_SCRATCH_ROOT / session_name / "claude-config")
        config.create(project.path, Path.home() / ".claude" / ".credentials.json")
        if not config.credentials_seeded:
            note = (
                "no file-based credentials found to seed; the fork will "
                "demand /login and the hooks wait will time out"
            )
            self._notes.append(note)
            print(f"    {note}")

        print("[4] forking claude in a detached tmux session")
        claude = Path(shutil.which("claude") or "claude")
        session = SessionLauncher(claude).launch(
            session_name, project, config, TaskSeed().derive()
        )
        print(f"    tmux session: {session.name}")

        print("[5] waiting for hooks over loopback")
        ledger = HookLedger(ledger_path)
        seen = self._await_hooks(session, ledger)
        print(f"    events seen: {sorted(seen)}")
        self._verdicts["hooks_land_ordered_attributed"] = self._judge_hooks(ledger)

        print("[6] capturing pane mid-run (non-interactive tmux attach)")
        mid = session.capture()
        (self._results_dir / "capture_mid_run.txt").write_text(mid, "utf-8")
        self._verdicts["attach_shows_usable_session"] = self._judge_pane(mid)

        print("[7] SIGKILL the store; fork must survive")
        self._verdicts["fork_survives_store_kill"] = self._survival_test(store, session)

        print("[8] teardown, twice, both clean")
        self._verdicts["teardown_idempotent"] = self._teardown_test(session)

    def _await_hooks(self, session: TmuxSession, ledger: HookLedger) -> set[str]:
        deadline = time.monotonic() + _HOOK_WAIT_S
        seen: set[str] = set()
        while time.monotonic() < deadline:
            self._nudge_dialogs(session)
            seen = {record.event for record in ledger.records()}
            if seen >= _REQUIRED_EVENTS:
                break
            time.sleep(_POLL_INTERVAL_S)
        return seen

    def _judge_hooks(self, ledger: HookLedger) -> bool:
        """PASS only for a COMPLETE, ordered, attributed ledger.

        Ordering/attribution alone is not enough: a run where the relay
        died after SessionStart would leave a perfectly ordered partial
        ledger. Completeness against the required event set gates first.

        Completeness is judged on a FRESH ledger read, never on the poll
        loop's last snapshot: an event landing in the final poll gap would
        otherwise make the verdict contradict the on-disk evidence.
        """
        seen = {record.event for record in ledger.records()}
        missing = _REQUIRED_EVENTS - seen
        if missing:
            note = f"hooks incomplete at timeout; missing: {sorted(missing)}"
            self._notes.append(note)
            print(f"    {note}")
            return False
        return self._judge_ledger(ledger)

    def _judge_pane(self, pane: str) -> bool:
        """PASS only when the pane shows the seeded task in progress."""
        if _PANE_TASK_MARKER in pane:
            return True
        note = f"mid-run pane lacks task marker {_PANE_TASK_MARKER!r}"
        self._notes.append(note)
        print(f"    {note}")
        return False

    def _nudge_dialogs(self, session: TmuxSession) -> None:
        if not session.alive():
            return
        try:
            pane = session.capture()
        except subprocess.CalledProcessError:
            # The pane vanished between the alive check and the capture --
            # the session is dying; the ledger poll will surface that.
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

    def _judge_ledger(self, ledger: HookLedger) -> bool:
        records = ledger.records()
        if not records:
            return False
        recv = [r.recv_seq for r in records]
        globally_ordered = recv == sorted(recv) and len(set(recv)) == len(recv)
        attributed = all(r.session_id != "unattributed" for r in records)
        per_session_ok = True
        counters: dict[str, int] = {}
        for record in records:
            expected = counters.get(record.session_id, 0) + 1
            per_session_ok = per_session_ok and record.session_seq == expected
            counters[record.session_id] = expected
        return globally_ordered and attributed and per_session_ok

    def _survival_test(self, store: StoreProcess, session: TmuxSession) -> bool:
        log: list[str] = []
        records_before = len(HookLedger(store.ledger_path).records())
        store.sigkill()
        log.append("store process group SIGKILLed")
        store_dead = store.confirmed_dead()
        log.append(f"store port refuses connections: {store_dead}")
        time.sleep(5)
        alive_after = session.alive()
        log.append(f"tmux session alive after kill: {alive_after}")
        after = session.capture() if alive_after else ""
        (self._results_dir / "capture_after_store_kill.txt").write_text(after, "utf-8")
        responded = False
        if alive_after:
            if _SURVIVAL_MARKER in _SURVIVAL_PROMPT:
                msg = "survival marker must not appear in the sent prompt"
                raise RuntimeError(msg)
            session.send_line(_SURVIVAL_PROMPT)
            responded = self._await_pane_marker(
                session, _SURVIVAL_MARKER, timeout_s=120
            )
            (self._results_dir / "capture_post_kill_turn.txt").write_text(
                session.capture(), "utf-8"
            )
        log.append(f"fork answered a post-kill turn: {responded}")
        records_after = len(HookLedger(store.ledger_path).records())
        log.append(
            f"ledger records before kill: {records_before}, "
            f"after post-kill turn: {records_after} "
            "(no growth proves the fork's relays failed harmlessly)"
        )
        (self._results_dir / "survival.log").write_text("\n".join(log), "utf-8")
        return store_dead and alive_after and responded

    def _await_pane_marker(
        self, session: TmuxSession, marker: str, timeout_s: int
    ) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if marker in session.capture():
                return True
            time.sleep(_POLL_INTERVAL_S)
        return False

    def _teardown_test(self, session: TmuxSession) -> bool:
        first = Teardown(_SCRATCH_ROOT).run()
        second = Teardown(_SCRATCH_ROOT).run()
        gone = not session.alive() and not _SCRATCH_ROOT.exists()
        log = ["-- first pass --", *first.log, "-- second pass --", *second.log]
        (self._results_dir / "teardown.log").write_text("\n".join(log), "utf-8")
        return first.clean and second.clean and gone

    def _teardown_safety_net(self) -> None:
        # Never leave a fork running, whatever failed above -- and never
        # discard the outcome: on an exception path a silently-failing
        # rmtree would otherwise leave the credentials copy unnamed.
        outcome = Teardown(_SCRATCH_ROOT).run()
        if not outcome.clean:
            for line in outcome.log:
                self._notes.append(f"safety-net teardown: {line}")
                print(f"    safety-net teardown: {line}", file=sys.stderr)

    def _write_verdict(self) -> int:
        overall = bool(self._verdicts) and all(self._verdicts.values())
        verdict = {
            "chain": "fork -> configure -> attach -> hook-loopback",
            "criteria": self._verdicts,
            "notes": self._notes,
            "nudges": self._nudges,
            "overall": "PASS" if overall else "FAIL",
        }
        (self._results_dir / "verdict.json").write_text(
            json.dumps(verdict, indent=2, sort_keys=True), "utf-8"
        )
        print(json.dumps(verdict, indent=2, sort_keys=True))
        return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(ValidationRun().execute())
