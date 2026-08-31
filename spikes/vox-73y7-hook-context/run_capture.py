# /// script
# requires-python = ">=3.13"
# dependencies = ["websockets>=14"]
# ///
"""The realism capture: one ~20-minute working session, hooks to the store.

One run forks ONE claude session (cap 2, retry only) into a scratch
project seeded with a failing test suite, relays every hook event through
the sender-stamped relay to the stub voxd store, and produces the bead's
evidence:

- ``hook_ledger.jsonl``          -- every relayed event, stamped both ends.
- ``timepoints.json`` + ``capture_<label>.txt`` -- four sampled moments
  (early, mid-debug, post-fix, end) with the ledger cutoff and the pane
  capture that is the grading ground truth.
- ``gap_window.json``            -- the mid-session store SIGKILL/restart
  window for the gap-detection question.
- ``field_inventory.json`` / ``latency.json`` / ``gap_report.json``
- ``reconstructions.txt`` / ``reconstructions.json`` -- the ledger-tail
  and seed-only "what was I just doing?" answers at every timepoint,
  rendered verbatim for grading.
- ``teardown.log``               -- two passes, both clean.

Run from this directory:  uv run run_capture.py
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

from field_inventory import FieldInventory
from gap_check import GapReport
from latency import LatencyReport
from launcher import SESSION_PREFIX, SessionLauncher, TmuxSession
from reconstructor import TailReconstructor, has_failure, has_success, response_text
from scratch import IsolatedConfig, ScratchProject
from seed_builder import SeedBuilder, SeedReconstructor
from session_task import SEEDED_FILES, WorkSessionTask
from stamp import HookLedger, HookRecord, Sanitizer
from teardown import Teardown
from wiring import CONTEXT_CAPTURE_V1, HookWiring, RelayScript, SettingsDocument

_SPIKE_DIR = Path(__file__).parent
# The fork's scratch (project + isolated claude-config) lives under the REPO
# root's gitignored .tmp/, not the spike's own: the fork's config dir pulls
# vendored plugin markdown, and repo `make check` markdownlints everything
# under spikes/ -- a live run inside the spike tree fails the docs gate until
# teardown. The repo-root .tmp/ is inside markdownlint's ignore list.
_SCRATCH_ROOT = _SPIKE_DIR.parent.parent / ".tmp" / "vox73y7-scratch"

# Interactive-UI chrome the fork may show before working; the poller answers
# each once, with the keystrokes listed, and logs the nudge. The
# workspace-trust dialog can appear despite the pre-seeded
# hasTrustDialogAccepted because the deposited settings.json pre-approves
# tools, which triggers a stronger variant whose default is "No, exit" --
# hence Down + Enter to select "Yes, I trust this folder".
_DIALOG_MARKERS: dict[str, tuple[str, ...]] = {
    "Yes, I trust this folder": ("Down", "Enter"),
    "Choose the text style": ("Enter",),
    "Press Enter to": ("Enter",),
    "Detected a custom API key": ("Enter",),
}

# The whole session budget; a realistic multi-file task with a debug loop
# runs ~15-25 minutes.
_RUN_DEADLINE_S = 2100
# If NOTHING lands in this window the fork never configured -- abort.
_FIRST_HOOK_WAIT_S = 240
_POLL_INTERVAL_S = 3.0
# How long the store stays dead mid-session (the loss window).
_GAP_WINDOW_S = 60
# Fallback: run the loss window this long into the session even if the
# post-fix trigger has not fired, so the gap evidence cannot be lost to a
# fork that dodges the seeded failure.
_GAP_FALLBACK_S = 420

# Committed artifacts are path-sanitized at persist time.
_HOST_SANITIZER = Sanitizer.for_host(_SCRATCH_ROOT)
_LOGIN_BANNER_MARKER = "Your login expires"
_LOGIN_BANNER_PLACEHOLDER = "<login-status banner removed>"

_TIMEPOINT_LABELS = ("early", "mid-debug", "post-fix", "end")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@final
class StoreProcess:
    """The stub voxd store as a killable, RESTARTABLE subprocess.

    The port is fixed at construction so a restart reuses the URL baked
    into the fork's settings; the ledger is append-only across restarts.
    """

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
        """Launch (or relaunch) the store and wait for store/health."""
        # start_new_session puts the `uv run` wrapper AND its python child
        # in one process group: killing only the wrapper's pid would
        # orphan the actual store and fake the gap evidence.
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

    def sigkill(self) -> None:
        """SIGKILL the store's whole process group -- the loss window opens."""
        if self._process is None:
            msg = "store was never started"
            raise RuntimeError(msg)
        os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
        self._process.wait(timeout=10)

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
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                print(
                    f"WARNING: store pid {self._process.pid} unreaped after "
                    "SIGKILL (uninterruptible sleep?)",
                    file=sys.stderr,
                )

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
            except (TimeoutError, OSError):
                await asyncio.sleep(0.5)
        msg = "hook store never became healthy"
        raise RuntimeError(msg)


@final
class TimepointSampler:
    """Watches the ledger for the four trigger conditions and samples.

    A sample is the pair the grading needs: the ledger cutoff (the
    FILE-ORDER record count at that instant -- recv_seq restarts when the
    store restarts, so it cannot bound a timepoint) and the pane capture
    that is ground truth for the same instant.
    """

    __slots__ = ("_results_dir", "_samples", "_session")

    _results_dir: Path
    _samples: dict[str, dict[str, object]]
    _session: TmuxSession

    def __new__(cls, session: TmuxSession, results_dir: Path) -> Self:
        self = super().__new__(cls)
        self._session = session
        self._results_dir = results_dir
        self._samples = {}
        return self

    @property
    def samples(self) -> dict[str, dict[str, object]]:
        """label -> {cutoff_index, sampled_at, capture_file}."""
        return self._samples

    def done(self) -> bool:
        """True once every timepoint is sampled."""
        return all(label in self._samples for label in _TIMEPOINT_LABELS)

    def observe(self, records: tuple[HookRecord, ...]) -> tuple[str, ...]:
        """Check triggers against the ledger; sample any that just fired."""
        fired = []
        for label in _TIMEPOINT_LABELS:
            if label in self._samples or not self._triggered(label, records):
                continue
            self._sample(label, records)
            fired.append(label)
        return tuple(fired)

    def _triggered(self, label: str, records: tuple[HookRecord, ...]) -> bool:
        if label == "early":
            return any(r.event == "PostToolUse" for r in records)
        if label == "mid-debug":
            # Strictly after early's cutoff: when the fork's FIRST action
            # is the failing suite, both triggers match the same record
            # and the two labels would otherwise collapse onto one cutoff.
            early_index = self._sampled_index("early")
            return early_index is not None and any(
                index > early_index
                and record.event == "PostToolUse"
                and has_failure(response_text(record))
                for index, record in enumerate(records, 1)
            )
        if label == "post-fix":
            failure_index = self._sampled_index("mid-debug")
            return failure_index is not None and any(
                index > failure_index
                and record.event == "PostToolUse"
                and has_success(response_text(record))
                for index, record in enumerate(records, 1)
            )
        return any(r.event == "Stop" for r in records)

    def _sampled_index(self, label: str) -> int | None:
        # None until the labeled timepoint has been sampled -- each
        # dependent timepoint is ordered strictly after its predecessor
        # via this lookup: mid-debug after early, post-fix after
        # mid-debug, so no two labels can alias onto one cutoff.
        sample = self._samples.get(label)
        if sample is None:
            return None
        cutoff = sample["cutoff_index"]
        return cutoff if isinstance(cutoff, int) else None

    def _sample(self, label: str, records: tuple[HookRecord, ...]) -> None:
        capture_name = f"capture_{label.replace('-', '_')}.txt"
        pane = self._session.capture() if self._session.alive() else "(session gone)"
        scrubbed = _HOST_SANITIZER.scrub(pane)
        body = "\n".join(
            _LOGIN_BANNER_PLACEHOLDER if _LOGIN_BANNER_MARKER in line else line
            for line in scrubbed.splitlines()
        )
        (self._results_dir / capture_name).write_text(body, "utf-8")
        self._samples[label] = {
            "cutoff_index": len(records),
            "sampled_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
            "capture_file": capture_name,
        }
        print(
            f"    sampled timepoint {label!r} at cutoff "
            f"{self._samples[label]['cutoff_index']}"
        )


@final
class CaptureRun:
    """One full realism capture: fork, relay, sample, gap, analyze."""

    __slots__ = ("_gap_window", "_notes", "_nudges", "_results_dir")

    _gap_window: dict[str, object]
    _notes: list[str]
    _nudges: list[str]
    _results_dir: Path

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        stamp_dir = datetime.now(tz=UTC).strftime("run_%Y%m%d_%H%M%S")
        self._results_dir = _SPIKE_DIR / "results" / stamp_dir
        self._notes = []
        self._nudges = []
        self._gap_window = {}
        return self

    def execute(self) -> int:
        """Run the whole capture; return a process exit code."""
        self._results_dir.mkdir(parents=True, exist_ok=True)
        print(f"[0] evidence dir: {self._results_dir}")
        print("[1] pre-run teardown (idempotent)")
        Teardown(_SCRATCH_ROOT).run()

        ledger_path = self._results_dir / "hook_ledger.jsonl"
        store = StoreProcess(_free_port(), ledger_path)
        ok = False
        try:
            ok = self._drive(store)
        finally:
            self._teardown()
            store.stop_if_running()
        self._write_summary(ok=ok)
        return 0 if ok else 1

    def _drive(self, store: StoreProcess) -> bool:
        print("[2] starting stub voxd store")
        store.start()
        print(f"    healthy at {store.url}")

        print("[3] scratch project + isolated config + relay deposit")
        session_name = f"{SESSION_PREFIX}-{datetime.now(tz=UTC):%H%M%S}"
        project = ScratchProject(_SCRATCH_ROOT / session_name / "project")
        config = IsolatedConfig(_SCRATCH_ROOT / session_name / "claude-config")
        proxy_on_path = shutil.which("mcp-proxy")
        if proxy_on_path is None:
            # Falling back to a bare "mcp-proxy" would turn an absent
            # binary into a generic four-minute first-hook timeout; fail
            # at setup with the actual cause instead.
            msg = "mcp-proxy not found on PATH; install it before a capture run"
            raise RuntimeError(msg)
        proxy = Path(proxy_on_path)
        relay_body = RelayScript(
            proxy, store.url, config.stamper_script, config.counter_dir
        ).render()
        settings = SettingsDocument(CONTEXT_CAPTURE_V1, HookWiring(config.relay_script))
        project.create(settings.render(), SEEDED_FILES)
        config.create(project.path, Path.home() / ".claude" / ".credentials.json")
        config.deposit_relay(relay_body)
        if not config.credentials_seeded:
            self._note("no file-based credentials to seed; fork will demand /login")

        print("[4] forking claude in a detached tmux session")
        claude = Path(shutil.which("claude") or "claude")
        session = SessionLauncher(claude).launch(
            session_name, project, config, WorkSessionTask().derive()
        )
        print(f"    tmux session: {session.name}")

        print("[5] polling: timepoints + mid-session gap window")
        sampler = TimepointSampler(session, self._results_dir)
        completed = self._poll(store, session, sampler)
        (self._results_dir / "timepoints.json").write_text(
            json.dumps(sampler.samples, indent=2, sort_keys=True), "utf-8"
        )
        if not completed:
            self._note(f"run ended with timepoints: {sorted(sampler.samples)}")

        print("[6] offline analysis over the final ledger")
        self._analyze(HookLedger(store.ledger_path).records(), sampler)
        return completed

    def _poll(
        self, store: StoreProcess, session: TmuxSession, sampler: TimepointSampler
    ) -> bool:
        ledger = HookLedger(store.ledger_path)
        started = time.monotonic()
        deadline = started + _RUN_DEADLINE_S
        first_hook_deadline = started + _FIRST_HOOK_WAIT_S
        gap_done = False
        gap_confirmed = False
        while time.monotonic() < deadline:
            self._nudge_dialogs(session)
            records = ledger.records_snapshot()
            if not records and time.monotonic() > first_hook_deadline:
                self._note("no hooks arrived in the first-hook window; aborting")
                return False
            fired = sampler.observe(records)
            # The loss window: kill the store while the fork keeps
            # working, then bring it back. Preferred trigger is the
            # post-fix sample (the fork is mid-feature-work after it);
            # the elapsed-time fallback guarantees the gap evidence even
            # if a trigger misfires, as long as the session is producing.
            gap_due = "post-fix" in fired or (
                "early" in sampler.samples
                and time.monotonic() - started > _GAP_FALLBACK_S
            )
            if gap_due and not gap_done and session.alive():
                gap_confirmed = self._gap(store)
                gap_done = True
                if not gap_confirmed:
                    # A kill that never took means the "loss window" saw
                    # no loss and gap_report.json's lost=0 would be a
                    # false all-clear -- the run must FAIL, not pass.
                    self._note("gap window kill NOT confirmed dead; run fails")
            if sampler.done():
                if not gap_done:
                    self._note("gap window never ran (post-fix missed)")
                return gap_done and gap_confirmed
            if not session.alive():
                self._note("tmux session died before all timepoints sampled")
                return False
            time.sleep(_POLL_INTERVAL_S)
        self._note("run deadline reached before all timepoints sampled")
        return False

    def _gap(self, store: StoreProcess) -> bool:
        """Run the loss window; True only when the port confirmed dead."""
        print("    [gap] SIGKILL store; loss window opens")
        killed_at = datetime.now(tz=UTC).isoformat(timespec="seconds")
        store.sigkill()
        dead = store.confirmed_dead()
        time.sleep(_GAP_WINDOW_S)
        print("    [gap] restarting store; loss window closes")
        store.start()
        self._gap_window = {
            "killed_at": killed_at,
            "confirmed_dead": dead,
            "window_s": _GAP_WINDOW_S,
            "restarted_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        }
        (self._results_dir / "gap_window.json").write_text(
            json.dumps(self._gap_window, indent=2, sort_keys=True), "utf-8"
        )
        return dead

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

    def _analyze(
        self, records: tuple[HookRecord, ...], sampler: TimepointSampler
    ) -> None:
        inventory = FieldInventory(records)
        self._write_json("field_inventory.json", inventory.as_dict())
        print(inventory.table())
        latency = LatencyReport(records)
        self._write_json("latency.json", latency.as_dict())
        print(latency.table())
        gaps = GapReport(records)
        self._write_json("gap_report.json", gaps.as_dict())
        print(gaps.summary())
        self._reconstruct(records, sampler)

    def _reconstruct(
        self, records: tuple[HookRecord, ...], sampler: TimepointSampler
    ) -> None:
        rendered: list[str] = []
        machine: list[dict[str, object]] = []
        for label, sample in sampler.samples.items():
            cutoff = sample["cutoff_index"]
            if not isinstance(cutoff, int):
                continue
            tail_answer = TailReconstructor(records, cutoff).answer(label)
            seed = SeedBuilder(records, cutoff).build()
            seed_answer = SeedReconstructor(seed).answer(label)
            rendered.extend((tail_answer.render(), "", seed_answer.render(), ""))
            machine.append(
                {
                    "timepoint": label,
                    "cutoff_index": cutoff,
                    "seed_bytes": seed.byte_size(),
                    "ledger_tail": tail_answer.render(),
                    "seed_only": seed_answer.render(),
                }
            )
        (self._results_dir / "reconstructions.txt").write_text(
            "\n".join(rendered), "utf-8"
        )
        self._write_json("reconstructions.json", {"answers": machine})

    def _teardown(self) -> None:
        first = Teardown(_SCRATCH_ROOT).run()
        second = Teardown(_SCRATCH_ROOT).run()
        log = ["-- first pass --", *first.log, "-- second pass --", *second.log]
        (self._results_dir / "teardown.log").write_text(
            _HOST_SANITIZER.scrub("\n".join(log)), "utf-8"
        )
        if not (first.clean and second.clean):
            self._note("teardown left leftovers; see teardown.log")

    def _write_json(self, name: str, body: dict[str, object]) -> None:
        (self._results_dir / name).write_text(
            json.dumps(body, indent=2, sort_keys=True) + "\n", "utf-8"
        )

    def _note(self, note: str) -> None:
        self._notes.append(note)
        print(f"    NOTE: {note}")

    def _write_summary(self, *, ok: bool) -> None:
        self._write_json(
            "run_summary.json",
            {
                "completed": ok,
                "notes": self._notes,
                "nudges": self._nudges,
                "gap_window": self._gap_window,
            },
        )
        print(f"run {'COMPLETED' if ok else 'INCOMPLETE'}; notes: {self._notes}")


if __name__ == "__main__":
    sys.exit(CaptureRun().execute())
