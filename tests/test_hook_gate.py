"""Behavioral tests for the per-repo enablement gate in the audio hooks.

Each audio session hook is silent unless the repo opted in with `vox enable`
(the `.punt-labs/vox/enabled` marker) AND the `vox` CLI is installed. These
tests drive the real shell scripts as subprocesses and assert the three gate
outcomes: marker present -> the hook reaches vox; marker absent -> silent
exit 0; vox absent -> silent exit 0. A stub `vox` on PATH records whether the
hook got past the gate, so no real synthesis runs.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).resolve().parent.parent / "plugin" / "hooks"

# Every test spawns the real hook/session-start shell scripts as subprocesses.
pytestmark = pytest.mark.subprocess

# hook script -> the hook_event_name it dispatches on. subagent.sh only calls
# vox for Subagent* events; the rest ignore the event but a realistic value is
# passed anyway.
_GATED_HOOKS = {
    "notify.sh": "Stop",
    "notify-permission.sh": "Notification",
    "acknowledge.sh": "UserPromptSubmit",
    "vibe-nudge.sh": "UserPromptSubmit",
    "pre-compact.sh": "PreCompact",
    "farewell.sh": "SessionEnd",
    "subagent.sh": "SubagentStop",
}


def _write_vox_stub(bin_dir: Path, sentinel: Path) -> None:
    """Install a fake `vox` in bin_dir that touches sentinel when invoked."""
    stub = bin_dir / "vox"
    stub.write_text(
        f'#!/usr/bin/env bash\nprintf invoked > "{sentinel}"\n',
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_failing_git(bin_dir: Path) -> None:
    """Install a `git` stub on PATH that always exits non-zero.

    Simulates git being unavailable (or the cwd not being a repo) so the hook
    gate falls back to walking parent directories for the marker.
    """
    stub = bin_dir / "git"
    stub.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _path_without_vox() -> str:
    """The current PATH with every directory that contains a `vox` removed."""
    entries = os.environ.get("PATH", "").split(os.pathsep)
    return os.pathsep.join(d for d in entries if not (Path(d) / "vox").exists())


def _run_hook(name: str, cwd: str, event: str, env: dict[str, str]) -> int:
    payload = json.dumps({"cwd": cwd, "hook_event_name": event})
    result = subprocess.run(
        ["bash", str(_HOOKS_DIR / name)],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    return result.returncode


def _make_repo(parent: Path) -> Path:
    # A standalone git worktree so `git rev-parse` stops here rather than
    # walking up to the enclosing repo (pytest's tmp lives under the repo tree).
    repo = parent / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def _mark_enabled(repo: Path) -> None:
    marker = repo / ".punt-labs" / "vox" / "enabled"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("", encoding="utf-8")


def _poll_until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> None:
    """Poll *predicate* until it's true -- the spawn is backgrounded
    (``nohup ... & disown``), so its effects land some time after the hook
    process itself has already exited, with no parent left to bound the wait.

    10s, not 2s: a 15-run isolated rerun of this poll (no concurrent
    load) still missed a 2s deadline once, so 2s is not a safe bound for
    OS scheduling of an already-disowned background process even at
    rest. The predicate resolves in milliseconds on the happy path --
    this loop returns as soon as it's true -- so a generous ceiling costs
    nothing when the spawn is prompt and only matters on the rare slow
    tail. 10s matches this file's own tolerance for hook subprocess
    timing elsewhere (``subprocess.run(..., timeout=30)`` below).
    """
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            msg = "timed out waiting for the backgrounded panel spawn"
            raise AssertionError(msg)
        time.sleep(0.02)


def _path_without_vox_panel() -> str:
    """The current PATH with every directory that contains `vox-panel` removed."""
    entries = os.environ.get("PATH", "").split(os.pathsep)
    return os.pathsep.join(d for d in entries if not (Path(d) / "vox-panel").exists())


@pytest.fixture
def isolated_root() -> Iterator[Path]:
    """A directory tree outside the repo, immune to the parent-marker walk.

    `.envrc` pins `TMPDIR` to `<repo>/.tmp/`, so pytest's `tmp_path` fixture
    nests under the real vox repo. A test asserting "no marker anywhere up to
    real /" would otherwise cross this project's own committed
    `.punt-labs/vox/enabled` marker and fail. Building under `/tmp` directly
    (bypassing `TMPDIR`) keeps the ancestor chain genuinely marker-free.
    """
    root = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


class TestHookGate:
    @pytest.mark.parametrize("name", sorted(_GATED_HOOKS))
    def test_enabled_repo_reaches_vox(self, name: str, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        _mark_enabled(repo)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        sentinel = tmp_path / "sentinel"
        _write_vox_stub(bin_dir, sentinel)
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"

        rc = _run_hook(name, str(repo), _GATED_HOOKS[name], env)

        assert rc == 0
        assert sentinel.exists(), f"{name} did not reach vox in an enabled repo"

    @pytest.mark.parametrize("name", sorted(_GATED_HOOKS))
    def test_disabled_repo_is_silent(self, name: str, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)  # no marker written
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        sentinel = tmp_path / "sentinel"
        _write_vox_stub(bin_dir, sentinel)
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"

        rc = _run_hook(name, str(repo), _GATED_HOOKS[name], env)

        assert rc == 0
        assert not sentinel.exists(), f"{name} fired in a non-enabled repo"

    @pytest.mark.parametrize("name", sorted(_GATED_HOOKS))
    def test_missing_vox_is_silent(self, name: str, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        _mark_enabled(repo)  # enabled, but vox is not installed
        env = dict(os.environ)
        env["PATH"] = _path_without_vox()

        rc = _run_hook(name, str(repo), _GATED_HOOKS[name], env)

        assert rc == 0

    def test_marker_resolved_by_walking_parents_when_git_unavailable(
        self, tmp_path: Path
    ) -> None:
        # git is stubbed to fail (unavailable / not a repo). From a subdir, the
        # gate must walk parents for the marker instead of stopping at cwd -- a
        # cwd-only fallback would wrongly suppress audio in an enabled repo.
        root = tmp_path / "tree"
        marker = root / ".punt-labs" / "vox" / "enabled"
        marker.parent.mkdir(parents=True)
        marker.write_text("", encoding="utf-8")
        subdir = root / "src" / "nested"
        subdir.mkdir(parents=True)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        sentinel = tmp_path / "sentinel"
        _write_vox_stub(bin_dir, sentinel)
        _write_failing_git(bin_dir)
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"

        rc = _run_hook("notify.sh", str(subdir), "Stop", env)

        assert rc == 0
        assert sentinel.exists(), "gate did not walk parents to the marker"

    def test_git_unavailable_and_no_marker_is_silent(self, isolated_root: Path) -> None:
        # git stubbed to fail and no marker in any parent: the parent walk bottoms
        # out at "/" and the hook exits silently rather than firing. Built under
        # isolated_root (real /tmp), not tmp_path, so the walk can't cross this
        # project's own committed enabled marker on its way to "/".
        subdir = isolated_root / "tree" / "src" / "nested"
        subdir.mkdir(parents=True)
        bin_dir = isolated_root / "bin"
        bin_dir.mkdir()
        sentinel = isolated_root / "sentinel"
        _write_vox_stub(bin_dir, sentinel)
        _write_failing_git(bin_dir)
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"

        rc = _run_hook("notify.sh", str(subdir), "Stop", env)

        assert rc == 0
        assert not sentinel.exists(), "hook fired with no marker in any parent"

    def test_marker_resolved_from_git_root_of_subdir(self, tmp_path: Path) -> None:
        # cwd is a nested subdir; the marker lives at the git worktree root.
        # The gate must resolve the root via `git rev-parse`, not just cwd.
        repo = _make_repo(tmp_path)
        _mark_enabled(repo)
        subdir = repo / "src" / "nested"
        subdir.mkdir(parents=True)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        sentinel = tmp_path / "sentinel"
        _write_vox_stub(bin_dir, sentinel)
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"

        rc = _run_hook("notify.sh", str(subdir), "Stop", env)

        assert rc == 0
        assert sentinel.exists(), "gate did not resolve the marker from git root"


class TestPanelSpawn:
    """``session-start.sh``'s vox-panel spawn block: a different shape from the
    simple gated hooks above (it also provisions permissions/commands), so
    each test isolates ``HOME`` to a scratch directory rather than reusing
    ``_run_hook``'s plain env."""

    @pytest.fixture(autouse=True)
    def _reap_panel_stubs(self) -> Iterator[None]:
        """Kill any stub this test spawned once it finishes.

        Every stub in this class shares one pgrep pattern -- `$PPID` is this
        pytest process's own pid, fixed for the whole run -- so a slow
        3-second sleeper left over from one test would otherwise satisfy the
        *next* test's pgrep guard check and make it look like nothing spawned.
        """
        yield
        subprocess.run(
            ["pkill", "-f", f"vox-panel --session-pid {os.getpid()}"],
            check=False,
            capture_output=True,
        )

    @staticmethod
    def _write_vox_panel_stub(bin_dir: Path) -> None:
        """A `vox-panel` stub that records every invocation, then stays alive
        long enough for a second hook run's `pgrep` guard to find it."""
        stub = bin_dir / "vox-panel"
        stub.write_text(
            '#!/usr/bin/env bash\necho "$$ $*" >> "$VOX_PANEL_SENTINEL"\nsleep 3\n',
            encoding="utf-8",
        )
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    @staticmethod
    def _base_env(tmp_path: Path) -> dict[str, str]:
        env = dict(os.environ)
        home = tmp_path / "home"
        home.mkdir()
        env["HOME"] = str(home)
        env["TMPDIR"] = str(tmp_path)
        return env

    @staticmethod
    def _panel_log(env: dict[str, str]) -> Path:
        """Where the hook logs this session's panel reasons.

        Under ``$HOME``, deliberately: a shared tmp directory is writable by
        every local user, so a predictable per-session filename there can be
        pre-empted by a symlink planted at that path before the hook runs.
        """
        home = Path(env["HOME"])
        return home / ".punt-labs" / "vox" / "logs" / f"vox-panel-{os.getpid()}.log"

    @staticmethod
    def _run_session_start(cwd: Path, env: dict[str, str]) -> int:
        payload = json.dumps({"cwd": str(cwd)})
        result = subprocess.run(
            ["bash", str(_HOOKS_DIR / "session-start.sh")],
            input=payload,
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        return result.returncode

    def test_spawns_when_enabled_and_vox_panel_present(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        _mark_enabled(repo)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        sentinel = tmp_path / "sentinel.log"
        self._write_vox_panel_stub(bin_dir)
        env = self._base_env(tmp_path)
        env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        env["VOX_PANEL_SENTINEL"] = str(sentinel)

        rc = self._run_session_start(repo, env)

        assert rc == 0
        _poll_until(sentinel.exists)
        assert f"--session-pid {os.getpid()}" in sentinel.read_text(encoding="utf-8")

    def test_pgrep_guard_skips_a_second_spawn(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        _mark_enabled(repo)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        sentinel = tmp_path / "sentinel.log"
        self._write_vox_panel_stub(bin_dir)
        env = self._base_env(tmp_path)
        env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        env["VOX_PANEL_SENTINEL"] = str(sentinel)

        assert self._run_session_start(repo, env) == 0
        _poll_until(sentinel.exists)  # the first spawn is now alive (sleep 3)

        assert self._run_session_start(repo, env) == 0

        lines = sentinel.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1, "the pgrep guard let a second panel spawn"
        log = self._panel_log(env)
        assert "already served" in log.read_text(encoding="utf-8")

    def test_spawns_when_cwd_is_not_a_git_working_tree(
        self, tmp_path: Path, isolated_root: Path
    ) -> None:
        """A session started outside any git working tree still gets a panel.

        `git rev-parse` exits non-zero there, and under `set -euo pipefail` a
        bare assignment from a failing command substitution aborts the whole
        script -- so every line below it, the spawn included, never ran.
        """
        _mark_enabled(isolated_root)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        sentinel = tmp_path / "sentinel.log"
        self._write_vox_panel_stub(bin_dir)
        env = self._base_env(tmp_path)
        env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        env["VOX_PANEL_SENTINEL"] = str(sentinel)

        rc = self._run_session_start(isolated_root, env)

        assert rc == 0, "the hook aborted before reaching the panel spawn"
        _poll_until(sentinel.exists)
        assert f"--session-pid {os.getpid()}" in sentinel.read_text(encoding="utf-8")

    def test_malformed_stdin_does_not_abort_the_hook(self, tmp_path: Path) -> None:
        """Non-JSON stdin leaves `$_cwd` unresolvable, not the script dead.

        `jq` (and the jq-less `grep` fallback) exit non-zero on input with no
        `cwd`; under `set -e` that killed the hook before its command
        deployment and permission auto-allow ever ran.
        """
        env = self._base_env(tmp_path)
        result = subprocess.run(
            ["bash", str(_HOOKS_DIR / "session-start.sh")],
            input="not json at all",
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        assert result.returncode == 0, "malformed stdin aborted the hook"

    def test_unreadable_stdin_does_not_abort_the_hook(self, tmp_path: Path) -> None:
        """An unreadable stdin costs the session its panel, not just its cwd.

        `cat` exits non-zero when fd 0 cannot be read from, and under `set -e`
        the bare assignment on line 1 killed the hook upstream of every other
        fallback -- the cwd default, command deployment, and the panel spawn
        alike. A write-only fd reproduces it without the deadlock a *closed*
        fd 0 causes: bash would hand the command substitution's own pipe to
        `cat` as its stdin, and `cat` would wait forever on itself.
        """
        write_only = os.open(tmp_path / "sink", os.O_WRONLY | os.O_CREAT, 0o600)
        env = self._base_env(tmp_path)
        try:
            result = subprocess.run(
                ["bash", str(_HOOKS_DIR / "session-start.sh")],
                stdin=write_only,
                capture_output=True,
                text=True,
                env=env,
                check=False,
                timeout=30,
            )
        finally:
            os.close(write_only)

        assert result.returncode == 0, "unreadable stdin aborted the hook"

    def test_relative_cwd_does_not_hang_the_parent_walk(self, tmp_path: Path) -> None:
        """A relative `cwd` in the payload must not spin the marker search.

        The walk up to the enablement marker stops at "/", which a relative
        path never reaches -- `dirname .` is `.` -- so an unnormalized
        relative cwd looped forever, hanging session startup outright rather
        than aborting it. The subprocess is run from a directory with no
        marker of its own, since a marker at `.` would end the walk on its
        first step and hide the defect.
        """
        env = self._base_env(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        result = subprocess.run(
            ["bash", str(_HOOKS_DIR / "session-start.sh")],
            input=json.dumps({"cwd": "relative/nonexistent/path"}),
            text=True,
            capture_output=True,
            cwd=elsewhere,
            env=env,
            check=False,
            timeout=30,
        )

        assert result.returncode == 0

    def test_logs_a_reason_when_vox_panel_is_missing(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        _mark_enabled(repo)
        env = self._base_env(tmp_path)
        env["PATH"] = _path_without_vox_panel()

        rc = self._run_session_start(repo, env)

        assert rc == 0
        log = self._panel_log(env)
        assert log.exists(), "no reason was logged for the missing vox-panel"
        assert "not found on PATH" in log.read_text(encoding="utf-8")

    def test_panel_log_avoids_the_world_writable_temp_dir(self, tmp_path: Path) -> None:
        """The panel log belongs under `$HOME`, in a 0700 directory.

        A shared temp directory is writable by every local user, so the
        predictable `vox-panel-<pid>.log` name could be pre-empted there by a
        symlink planted before the session started, and the hook's appends
        would land wherever that symlink pointed. Owning the directory removes
        the race outright -- no other user can create a path inside `$HOME`.
        """
        repo = _make_repo(tmp_path)
        _mark_enabled(repo)
        env = self._base_env(tmp_path)
        env["PATH"] = _path_without_vox_panel()

        assert self._run_session_start(repo, env) == 0

        log = self._panel_log(env)
        assert log.exists()
        assert stat.S_IMODE(log.parent.stat().st_mode) == 0o700
        shared = Path(env["TMPDIR"])
        assert not list(shared.glob("vox-panel-*.log")), (
            "the hook still writes a predictable name into the shared temp dir"
        )

    def test_unwritable_home_costs_the_log_not_the_hook(self, tmp_path: Path) -> None:
        """A log path that cannot be created must not abort session startup.

        The `>>` redirects onto the panel log are unguarded, and a failed
        redirect on a synchronous command aborts under `set -e`. So an
        unwritable `$HOME` -- where the log directory cannot be created --
        used to kill the hook on the very line that explains why the panel is
        missing, taking command deployment and the spawn down with it.
        """
        repo = _make_repo(tmp_path)
        _mark_enabled(repo)
        env = self._base_env(tmp_path)
        env["PATH"] = _path_without_vox_panel()
        home = Path(env["HOME"])
        home.chmod(0o500)
        try:
            rc = self._run_session_start(repo, env)
        finally:
            home.chmod(0o700)

        assert rc == 0, "an unwritable log path aborted the hook"


class TestManifestRefusal:
    """``session-start.sh`` refuses to guess dev-vs-prod it cannot read.

    The manifest decides the permission glob, the retired-command cleanup, and
    the command deployment. An absent or unreadable one used to fall through to
    *prod*, the more invasive branch, on the strength of a file the hook never
    managed to read -- so a wrong ``PLUGIN_ROOT`` deployed commands and wrote
    the prod glob instead of reporting the problem. These tests pin the third
    state: report it, touch nothing, exit 0.
    """

    @staticmethod
    def _plugin_copy(tmp_path: Path) -> Path:
        """Copy the real surface so its manifest can be broken safely."""
        dest = tmp_path / "plugin"
        shutil.copytree(_HOOKS_DIR.parent, dest)
        return dest

    @staticmethod
    def _run(plugin: Path, cwd: Path, home: Path) -> subprocess.CompletedProcess[str]:
        home.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["HOME"] = str(home)
        env["TMPDIR"] = str(home)
        return subprocess.run(
            ["bash", str(plugin / "hooks" / "session-start.sh")],
            input=json.dumps({"cwd": str(cwd)}),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def test_intact_manifest_still_provisions(self, tmp_path: Path) -> None:
        # The control. Without it, "nothing was written" below could just mean
        # the fixture is inert rather than that the guard fired.
        plugin = self._plugin_copy(tmp_path)
        result = self._run(plugin, _make_repo(tmp_path), tmp_path / "home")
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "home" / ".claude" / "settings.json").exists()

    def test_absent_manifest_provisions_nothing(self, tmp_path: Path) -> None:
        plugin = self._plugin_copy(tmp_path)
        (plugin / ".claude-plugin" / "plugin.json").unlink()
        home = tmp_path / "home"
        result = self._run(plugin, _make_repo(tmp_path), home)

        assert result.returncode == 0, result.stderr
        assert not (home / ".claude").exists(), "provisioned against an unread manifest"
        assert "No plugin manifest" in result.stdout
        assert "skipped command deployment and permission setup" in result.stdout

    def test_unreadable_manifest_provisions_nothing(self, tmp_path: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip("root bypasses the mode bits, so chmod 000 stays readable")

        plugin = self._plugin_copy(tmp_path)
        manifest = plugin / ".claude-plugin" / "plugin.json"
        manifest.chmod(0o000)
        home = tmp_path / "home"
        try:
            result = self._run(plugin, _make_repo(tmp_path), home)
        finally:
            manifest.chmod(0o644)

        assert result.returncode == 0, result.stderr
        assert not (home / ".claude").exists(), "provisioned against an unread manifest"
        # grep exits 2 on a read error and 1 on a genuine no-match; collapsing
        # the two is what made an unreadable manifest look like a prod one.
        assert "grep exit 2" in result.stdout
        assert "Permission denied" in result.stderr
