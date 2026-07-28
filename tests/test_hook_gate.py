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
import stat
import subprocess
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"

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

    def test_git_unavailable_and_no_marker_is_silent(self, tmp_path: Path) -> None:
        # git stubbed to fail and no marker in any parent: the parent walk bottoms
        # out at "/" and the hook exits silently rather than firing.
        subdir = tmp_path / "tree" / "src" / "nested"
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
