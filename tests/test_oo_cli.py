"""Unit tests for argument parsing and the CLI entry point."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools.oo_ratchet.cli import Options, main

# Shares a real git-tmp-repo fixture with test_oo_ratchet.py -- see the
# marker rationale there and the measured timing in TESTING.md.
pytestmark = pytest.mark.slow

GOOD = '''from __future__ import annotations


class Widget:
    """A widget."""

    _n: int

    def __new__(cls, n: int) -> "Widget":
        self = super().__new__(cls)
        self._n = n
        return self

    def label(self) -> str:
        return "pos"
'''


class TestOptionsParse:
    """argparse replaces the old substring argv sniffing (F6)."""

    def test_defaults(self) -> None:
        opts = Options.parse(["src/pkg"])
        assert opts.src == Path("src/pkg")
        assert not opts.check
        assert opts.base_ref is None
        assert not opts.require_base
        assert opts.justify == ""

    def test_check_with_base_ref_and_require(self) -> None:
        opts = Options.parse(
            ["src/pkg", "--check", "--base-ref", "abc123", "--require-base"]
        )
        assert opts.check
        assert opts.base_ref == "abc123"
        assert opts.require_base

    def test_relax_and_justify(self) -> None:
        opts = Options.parse(
            ["src/pkg", "--relax", "src/pkg/m.py", "--justify", "accepted"]
        )
        assert opts.relax == "src/pkg/m.py"
        assert opts.justify == "accepted"

    def test_allow_ci_write_and_source(self) -> None:
        opts = Options.parse(
            ["src", "--reconcile", "--allow-ci-write", "--source", "vox-1"]
        )
        assert opts.reconcile
        assert opts.allow_ci_write
        assert opts.source == "vox-1"


class TestActionExclusivity:
    """Action flags are mutually exclusive -- two is an error, not a pick."""

    def test_two_actions_is_argparse_error(self) -> None:
        with pytest.raises(SystemExit):
            Options.parse(["src", "--check", "--update"])

    def test_relax_and_reconcile_conflict(self) -> None:
        with pytest.raises(SystemExit):
            Options.parse(["src", "--relax", "src/m.py", "--reconcile"])

    def test_single_action_still_parses(self) -> None:
        assert Options.parse(["src", "--check"]).check
        assert Options.parse(["src", "--reconcile"]).reconcile
        assert Options.parse(["src", "--audit-completeness"]).audit_completeness

    def test_log_and_json_are_actions_not_silently_ignored(self) -> None:
        # --log/--json are views: combining with another action is an error,
        # not a silent no-op (Copilot #2).
        with pytest.raises(SystemExit):
            Options.parse(["src", "--check", "--log"])
        with pytest.raises(SystemExit):
            Options.parse(["src", "--check", "--json"])
        assert Options.parse(["src", "--log"]).log
        assert Options.parse(["src", "--json"]).json


class TestMainEntry:
    """The entry point scores and returns an exit code."""

    def test_json_mode_returns_zero_for_clean_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "good.py").write_text(GOOD)
        monkeypatch.chdir(tmp_path)
        assert main(["sub", "--json"]) == 0

    def test_missing_target_returns_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert main(["does-not-exist", "--json"]) == 1
