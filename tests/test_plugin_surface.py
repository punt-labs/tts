"""Behavioral tests for scripts/check-plugin-surface.sh.

A marketplace install fetches only the `plugin/` directory, so a
``${CLAUDE_PLUGIN_ROOT}``-relative path that resolves outside it, or to a file
the surface does not ship, is invisible in the source tree and broken on every
installed copy. The gate exists to catch that class; these tests drive it as a
subprocess against fixture surfaces and assert it actually rejects each shape,
because a guard that never fires is indistinguishable from no guard at all.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check-plugin-surface.sh"


def _run(surface: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT), str(surface)],
        text=True,
        capture_output=True,
        check=False,
    )


def _make_surface(root: Path, *, hook: str = "notify.sh") -> Path:
    """Build a minimal well-formed surface: a manifest, a command, one hook."""
    surface = root / "plugin"
    (surface / ".claude-plugin").mkdir(parents=True)
    (surface / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "fixture"}), encoding="utf-8"
    )
    (surface / "commands").mkdir()
    (surface / "commands" / "thing.md").write_text("do a thing\n", encoding="utf-8")
    hooks = surface / "hooks"
    hooks.mkdir()
    _write_hooks_json(hooks, f"${{CLAUDE_PLUGIN_ROOT}}/hooks/{hook}")
    script = hooks / hook
    script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return surface


def _write_hooks_json(hooks_dir: Path, command: str) -> None:
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}},
            indent=2,
        ),
        encoding="utf-8",
    )


class TestAcceptsAWellFormedSurface:
    def test_clean_surface_passes(self, tmp_path: Path) -> None:
        result = _run(_make_surface(tmp_path))
        assert result.returncode == 0, result.stderr
        assert "all resolve inside plugin/" in result.stdout

    def test_the_real_surface_passes(self) -> None:
        # No argument: the gate defaults to this repo's own plugin/ directory.
        result = subprocess.run(
            ["bash", str(_SCRIPT)], text=True, capture_output=True, check=False
        )
        assert result.returncode == 0, result.stderr


class TestRejectsAReferenceTheSurfaceCannotSatisfy:
    def test_missing_target_fails(self, tmp_path: Path) -> None:
        surface = _make_surface(tmp_path)
        (surface / "hooks" / "notify.sh").unlink()
        result = _run(surface)
        assert result.returncode == 1
        assert "does not ship" in result.stderr

    def test_reference_escaping_the_surface_fails(self, tmp_path: Path) -> None:
        # The prfaq failure shape: a runtime step reaching into a sibling
        # directory that cone mode leaves out of the install.
        surface = _make_surface(tmp_path)
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "helper.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        _write_hooks_json(surface / "hooks", "${CLAUDE_PLUGIN_ROOT}/../src/helper.sh")
        result = _run(surface)
        assert result.returncode == 1
        assert "escapes the plugin surface" in result.stderr

    def test_non_executable_hook_fails(self, tmp_path: Path) -> None:
        surface = _make_surface(tmp_path)
        script = surface / "hooks" / "notify.sh"
        script.chmod(script.stat().st_mode & ~0o111)
        result = _run(surface)
        assert result.returncode == 1
        assert "not executable" in result.stderr


class TestFailsClosed:
    def test_absent_surface_is_an_error(self, tmp_path: Path) -> None:
        result = _run(tmp_path / "nope")
        assert result.returncode == 2
        assert "plugin surface not found" in result.stderr

    def test_unmatched_hooks_json_is_an_error(self, tmp_path: Path) -> None:
        # If hooks.json stops carrying the placeholder, the extraction pattern
        # has rotted and every later check would pass vacuously. That is the
        # exact shape of the trailing-slash bug in restore-dev-plugin.sh: a
        # guard whose condition could never be true.
        surface = _make_surface(tmp_path)
        _write_hooks_json(surface / "hooks", "/absolute/path/notify.sh")
        result = _run(surface)
        assert result.returncode == 2
        assert "extraction pattern no longer matches" in result.stderr
