"""Behavioral tests for the PID resolution in plugin/hooks/session-start.sh.

Claude Code invokes SessionStart hooks through a short-lived ``sh`` wrapper:
the hook's own ``$PPID`` names that wrapper, not the long-running ``claude``
process that actually owns the session. Every ``vox-panel-*.log`` under
``~/.punt-labs/vox/logs`` contained exactly one line -- "session <pid> has
gone; the applet is leaving" -- because the panel was spawned watching
``$PPID`` directly, so its own liveness check saw the wrapper vanish within
seconds and left before ever reaching the Hub to register in the Lux menu.

These tests drive the real hook script as a subprocess at the bottom of a
real three-level process tree -- a stand-in ``claude`` process, a wrapper
that forks it away, and the hook script itself -- and assert it resolves the
*grandparent* pid, verified by an exact command-name match, rather than the
wrapper it was directly forked from. A fixture ``vox-panel`` stub on ``PATH``
captures the ``--session-pid`` argument the script actually passed, so this
is an outside-in check: nothing here reads the script's internals, only what
it does.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parent.parent / "plugin" / "hooks" / "session-start.sh"
)

pytestmark = [
    pytest.mark.subprocess,
    pytest.mark.skipif(shutil.which("jq") is None, reason="requires jq"),
    pytest.mark.skipif(shutil.which("ps") is None, reason="requires ps"),
    pytest.mark.skipif(shutil.which("pgrep") is None, reason="requires pgrep"),
]

_RELAY_SH = """#!/usr/bin/env bash
# Forks "$@" as a genuine child (backgrounding always forks) and waits for it,
# so each level in the tree is a distinct process with the expected parent.
# The explicit <&0 keeps the hook's stdin: without a redirection, a
# non-interactive bash gives an asynchronous command /dev/null as stdin, so
# the JSON payload written to the top of the tree never reached the hook --
# its cwd fell back to $PWD, the enablement-marker gate saw an unenabled
# directory, and the panel spawn these tests observe was silently skipped
# wherever $PWD did not nest under an enabled repo (CI, but not a local
# checkout whose TMPDIR-pinned tmp_path sits inside the real vox repo).
if [[ -n "${RELAY_TRACE_FILE:-}" ]]; then
  echo "$$" >> "$RELAY_TRACE_FILE"
fi
"$@" <&0 &
wait
"""


def _poll_until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> None:
    """Poll *predicate* until it's true -- the panel stub is spawned via
    ``nohup ... & disown`` (fire-and-forget), so it can still be writing its
    output file after the hook script -- and this test's ``subprocess.Popen``
    call -- has already returned. An immediate check races that write; a
    blind ``sleep`` only narrows the window without closing it.

    10s matches the equivalent poll in ``test_hook_gate.py``: a 15-run
    isolated rerun of that poll (no concurrent load) still missed a 2s
    deadline once, so 2s is not a safe bound for OS scheduling of an
    already-disowned background process even at rest. The predicate
    resolves in milliseconds on the happy path -- this loop returns as soon
    as it's true -- so the generous ceiling costs nothing when the spawn is
    prompt and only matters on the rare slow tail.
    """
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            msg = (
                "timed out waiting for the backgrounded vox-panel stub to write output"
            )
            raise AssertionError(msg)
        time.sleep(0.02)


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _make_named_binary(tmp_path: Path, name: str) -> Path:
    """Copy the real `bash` binary to *name* so its `ps` comm matches *name*.

    `/proc/[pid]/comm` reflects the basename of the execve'd file, not a
    script's shebang target -- invoking `bash script.sh` always reports comm
    `bash`. Renaming the binary itself is the only way to control comm.
    """
    bash_path = shutil.which("bash")
    assert bash_path is not None, "bash must be on PATH to build the fixture"
    dest = tmp_path / name
    shutil.copy(bash_path, dest)
    _make_executable(dest)
    return dest


def _system_path() -> str:
    return os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")


def _vox_panel_free_path() -> str:
    """The system PATH with every directory that contains `vox-panel` removed.

    Under `uv run` the project venv's bin -- which ships the real vox-panel --
    leads PATH, so an unstripped tail leaves the live binary one lookup miss
    away from the Lux hub. Same helper as test_hook_gate's
    `_path_without_vox_panel`, local to keep the test modules uncoupled.
    Drops each whole directory, so a host that colocates `vox-panel` with
    tools the hook needs (git, jq) loses those tools too -- a loud, if
    misdirecting, failure rather than a silent one. Only absolute entries are
    kept: relative entries ("", ".", "./x", "..") resolve against the hook's
    cwd, which could still resolve a `./vox-panel`.
    """
    entries = _system_path().split(os.pathsep)
    return os.pathsep.join(
        d
        for d in entries
        if (p := Path(d)).is_absolute() and not (p / "vox-panel").exists()
    )


def _run_tree(tmp_path: Path, *, claude_comm: str) -> tuple[int, int, int]:
    """Spawn claude(comm=claude_comm) -> wrapper -> session-start.sh.

    Returns (claude_pid, wrapper_pid, resolved_session_pid).
    """
    relay_sh = tmp_path / "relay.sh"
    relay_sh.write_text(_RELAY_SH, encoding="utf-8")
    _make_executable(relay_sh)

    claude_bin = _make_named_binary(tmp_path, claude_comm)

    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir()
    stub_out = tmp_path / "stub_out.txt"
    (stub_bin / "vox-panel").write_text(
        f'#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "{stub_out}"\n',
        encoding="utf-8",
    )
    _make_executable(stub_bin / "vox-panel")

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    # A standalone git repo, so the hook's `git -C` resolves its root HERE
    # rather than walking up to the enclosing (enabled) vox repo that pytest's
    # TMPDIR-pinned tmp_path nests under -- the spawn this fixture observes
    # must be gated on this tree's own marker, not the real repo's.
    repo_dir = tmp_path / "repo"
    (repo_dir / ".punt-labs" / "vox").mkdir(parents=True)
    (repo_dir / ".punt-labs" / "vox" / "enabled").touch()
    subprocess.run(["git", "init", "-q", str(repo_dir)], check=True)
    trace_file = tmp_path / "relay_trace.txt"

    env = dict(os.environ)
    env["HOME"] = str(fake_home)
    # Stub first, real vox-panel stripped from the tail: the spawn
    # this fixture captures must never be able to reach the live binary, even
    # if the stub is somehow skipped.
    env["PATH"] = f"{stub_bin}:{_vox_panel_free_path()}"
    env["RELAY_TRACE_FILE"] = str(trace_file)
    assert shutil.which("vox-panel", path=env["PATH"]) == str(stub_bin / "vox-panel"), (
        "the recording stub must be the only vox-panel this hook run can reach"
    )

    proc = subprocess.Popen(
        [str(claude_bin), str(relay_sh), "bash", str(relay_sh), "bash", str(_SCRIPT)],
        env=env,
        stdin=subprocess.PIPE,
        text=True,
        cwd=tmp_path,  # never inherit pytest's cwd (the enabled repo)
    )
    claude_pid = proc.pid
    proc.communicate(input=json.dumps({"cwd": str(repo_dir)}), timeout=15)
    assert proc.returncode == 0

    trace_lines = trace_file.read_text(encoding="utf-8").split()
    assert len(trace_lines) == 2, trace_lines
    wrapper_pid = int(trace_lines[1])

    _poll_until(stub_out.exists)
    stub_args = stub_out.read_text(encoding="utf-8").split()
    assert stub_args[:1] == ["--session-pid"], stub_args
    resolved_session_pid = int(stub_args[1])

    return claude_pid, wrapper_pid, resolved_session_pid


class TestResolvesTheRealSessionAcrossTheWrapper:
    def test_watches_the_grandparent_when_it_looks_like_claude(
        self, tmp_path: Path
    ) -> None:
        claude_pid, wrapper_pid, resolved = _run_tree(tmp_path, claude_comm="claude")
        assert resolved == claude_pid
        assert resolved != wrapper_pid

    def test_falls_back_to_the_wrapper_when_the_grandparent_is_unrecognized(
        self, tmp_path: Path
    ) -> None:
        claude_pid, wrapper_pid, resolved = _run_tree(
            tmp_path, claude_comm="not-recognized"
        )
        assert resolved == wrapper_pid
        assert resolved != claude_pid

    def test_rejects_a_near_miss_name_that_merely_contains_claude(
        self, tmp_path: Path
    ) -> None:
        """A substring match (`*claude*`) would wrongly trust this ancestor.

        `not-claude` contains the literal string "claude" but is not the real
        Claude Code process, so the exact-match check must reject it and fall
        back to the wrapper -- the same fail-safe outcome as any other
        unrecognized ancestor.
        """
        claude_pid, wrapper_pid, resolved = _run_tree(
            tmp_path, claude_comm="not-claude"
        )
        assert resolved == wrapper_pid
        assert resolved != claude_pid
