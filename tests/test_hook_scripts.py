"""Static assertions over the shell hook scripts in plugin/hooks/.

The shell scripts cannot be unit-tested in-process, so these checks
guard the two structural invariants the cwd fix relies on: the gate
references the current config filenames, and the dead daemon relay and
legacy config.md references are gone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).resolve().parent.parent / "plugin" / "hooks"
_SCRIPTS = (
    "notify.sh",
    "notify-permission.sh",
    "vibe-nudge.sh",
    "subagent.sh",
    "farewell.sh",
    "pre-compact.sh",
    "acknowledge.sh",
)


def _read(name: str) -> str:
    return (_HOOKS_DIR / name).read_text(encoding="utf-8")


class TestHookScripts:
    @pytest.mark.parametrize("name", _SCRIPTS)
    def test_no_dead_relay_or_legacy_config(self, name: str) -> None:
        text = _read(name)
        assert "mcp-proxy" not in text
        assert "serve.port" not in text
        assert "serve.token" not in text
        assert "config.md" not in text

    @pytest.mark.parametrize("name", _SCRIPTS)
    def test_gate_uses_enabled_marker(self, name: str) -> None:
        # Per-repo opt-in: an audio hook is silent unless the repo ran
        # `vox enable` (the marker) and the vox CLI is installed.
        text = _read(name)
        assert ".punt-labs/vox/enabled" in text
        assert "command -v vox" in text

    @pytest.mark.parametrize("name", _SCRIPTS)
    def test_gate_drops_retired_config_presence_check(self, name: str) -> None:
        # The marker supersedes the old vox.md/vox.local.md presence gate;
        # keeping it would fire audio in a disabled repo whose config is dormant.
        text = _read(name)
        assert ".punt-labs/vox/vox.md" not in text
        assert ".punt-labs/vox/vox.local.md" not in text

    @pytest.mark.parametrize("name", _SCRIPTS)
    def test_extracts_cwd_from_stdin(self, name: str) -> None:
        text = _read(name)
        assert ".cwd // empty" in text


class TestVibeNudgeHook:
    """The auto-vibe nudge must inject context, never block or run async."""

    def test_script_never_emits_a_decision(self) -> None:
        # Non-blocking by construction: blocking is the Stop hook's job.
        assert "decision" not in _read("vibe-nudge.sh")

    def test_registered_synchronously(self) -> None:
        # Only synchronous UserPromptSubmit stdout is injected as
        # additionalContext; an async registration would silently drop it.
        config = json.loads((_HOOKS_DIR / "hooks.json").read_text(encoding="utf-8"))
        entries = [
            hook
            for group in config["hooks"]["UserPromptSubmit"]
            for hook in group["hooks"]
            if hook["command"].endswith("vibe-nudge.sh")
        ]
        assert len(entries) == 1
        assert "async" not in entries[0]
