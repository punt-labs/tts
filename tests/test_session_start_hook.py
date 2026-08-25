"""Behavioral tests for plugin/hooks/session-start.sh command deployment.

vox-ovz3: model.md, provider.md, voice.md, and recap.md are namespaced-only
commands (``/vox:model``, never bare ``/model``) because a bare top-level
form collides with a name Claude Code itself may claim — ``/model`` already
does. The deploy loop must skip them, and the RETIRED cleanup must remove
any copy a prior session already deployed. The five session-scoped verbs
(``vox``, ``unmute``, ``mute``, ``vibe``, ``music``) are unaffected and must
keep deploying bare, unchanged.

Driven as a subprocess against the real script, with a copy of the real
``plugin/`` tree and a sandboxed ``$HOME`` — the interface is the contract,
so this exercises the shell, not a reimplementation of it. Only ``$HOME`` is
sandboxed; ``jq``, ``git``, and the coreutils the script shells out to run
from the real system ``PATH``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_SRC = _REPO_ROOT / "plugin"

pytestmark = [
    pytest.mark.subprocess,
    pytest.mark.skipif(
        shutil.which("jq") is None, reason="session-start.sh requires jq"
    ),
]

# The four commands that must deploy namespaced-only (never bare).
_NAMESPACED_ONLY = ("model", "provider", "voice", "recap")
# The five commands that must keep deploying bare, unaffected.
_BARE = ("vox", "unmute", "mute", "vibe", "music")


def _make_prod_plugin(root: Path) -> Path:
    """Copy the real plugin/ tree into ``root`` with a prod-named manifest.

    The hook's dev/prod branch reads whether ``.claude-plugin/plugin.json``
    contains the literal ``"vox-dev"``; a prod name is required for the
    deploy loop and RETIRED cleanup to run at all (dev mode skips both).
    """
    dest = root / "plugin"
    shutil.copytree(_PLUGIN_SRC, dest)
    manifest = dest / ".claude-plugin" / "plugin.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["name"] = "vox"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    return dest


def _run(plugin_dir: Path, home: Path) -> subprocess.CompletedProcess[str]:
    home.mkdir(parents=True, exist_ok=True)
    hook = plugin_dir / "hooks" / "session-start.sh"
    env = {"HOME": str(home), "PATH": _system_path()}
    return subprocess.run(
        ["bash", str(hook)],
        input=json.dumps({"cwd": str(home)}),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _system_path() -> str:
    return os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")


def _commands_dir(home: Path) -> Path:
    return home / ".claude" / "commands"


def _deployed_names(home: Path) -> set[str]:
    d = _commands_dir(home)
    if not d.is_dir():
        return set()
    return {p.name for p in d.glob("*.md")}


def _additional_context(result: subprocess.CompletedProcess[str]) -> str:
    if not result.stdout.strip():
        return ""
    payload = json.loads(result.stdout)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert isinstance(context, str)
    return context


class TestFreshInstallNamespacesTheFour:
    """A session with no prior deployed commands never acquires the bare four."""

    def test_namespaced_commands_are_not_deployed(self, tmp_path: Path) -> None:
        plugin_dir = _make_prod_plugin(tmp_path)
        result = _run(plugin_dir, tmp_path / "home")
        assert result.returncode == 0, result.stderr
        deployed = _deployed_names(tmp_path / "home")
        for name in _NAMESPACED_ONLY:
            assert f"{name}.md" not in deployed, (
                f"{name}.md deployed bare — collides with a name Claude Code may claim"
            )

    def test_bare_commands_still_deploy(self, tmp_path: Path) -> None:
        plugin_dir = _make_prod_plugin(tmp_path)
        result = _run(plugin_dir, tmp_path / "home")
        assert result.returncode == 0, result.stderr
        deployed = _deployed_names(tmp_path / "home")
        for name in _BARE:
            assert f"{name}.md" in deployed

    def test_deployed_bare_content_matches_source(self, tmp_path: Path) -> None:
        plugin_dir = _make_prod_plugin(tmp_path)
        home = tmp_path / "home"
        _run(plugin_dir, home)
        for name in _BARE:
            src = (plugin_dir / "commands" / f"{name}.md").read_text(encoding="utf-8")
            dest = (_commands_dir(home) / f"{name}.md").read_text(encoding="utf-8")
            assert src == dest


class TestStaleInstallIsRetired:
    """A `$HOME` already carrying the old bare four has them cleaned up."""

    def _seed_stale_commands(self, home: Path, plugin_dir: Path) -> None:
        commands_dir = _commands_dir(home)
        commands_dir.mkdir(parents=True, exist_ok=True)
        for name in (*_NAMESPACED_ONLY, *_BARE):
            src = plugin_dir / "commands" / f"{name}.md"
            (commands_dir / f"{name}.md").write_text(
                src.read_text(encoding="utf-8"), encoding="utf-8"
            )

    def test_retired_cleanup_removes_the_four(self, tmp_path: Path) -> None:
        plugin_dir = _make_prod_plugin(tmp_path)
        home = tmp_path / "home"
        self._seed_stale_commands(home, plugin_dir)
        result = _run(plugin_dir, home)
        assert result.returncode == 0, result.stderr
        deployed = _deployed_names(home)
        for name in _NAMESPACED_ONLY:
            assert f"{name}.md" not in deployed, (
                f"stale {name}.md was not retired from an existing install"
            )

    def test_retired_cleanup_leaves_the_bare_five(self, tmp_path: Path) -> None:
        plugin_dir = _make_prod_plugin(tmp_path)
        home = tmp_path / "home"
        self._seed_stale_commands(home, plugin_dir)
        result = _run(plugin_dir, home)
        assert result.returncode == 0, result.stderr
        deployed = _deployed_names(home)
        for name in _BARE:
            assert f"{name}.md" in deployed

    def test_hook_reports_the_cleanup(self, tmp_path: Path) -> None:
        plugin_dir = _make_prod_plugin(tmp_path)
        home = tmp_path / "home"
        self._seed_stale_commands(home, plugin_dir)
        result = _run(plugin_dir, home)
        context = _additional_context(result)
        assert "Cleaned retired commands" in context
        for name in _NAMESPACED_ONLY:
            assert f"/{name}" in context


class TestDevModeSkipsDeployment:
    """A vox-dev-named manifest never deploys or retires anything.

    The dev/prod split only runs command deployment in prod mode — dev mode
    relies on the sibling prod plugin (per session-start.sh's own comment).
    A namespace regression must not hide inside "well, dev mode never
    touched it anyway", so this pins that dev mode is a true no-op here.
    """

    def test_dev_manifest_deploys_nothing(self, tmp_path: Path) -> None:
        # _PLUGIN_SRC's own manifest already carries "vox-dev" in the
        # working tree, so a plain copy with no rename exercises dev mode.
        dest = tmp_path / "plugin"
        shutil.copytree(_PLUGIN_SRC, dest)
        home = tmp_path / "home"
        result = _run(dest, home)
        assert result.returncode == 0, result.stderr
        assert not _commands_dir(home).exists() or not _deployed_names(home)
