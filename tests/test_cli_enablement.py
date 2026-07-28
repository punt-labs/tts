"""Tests for the ``vox enable`` / ``vox disable`` CLI verbs."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from punt_vox.__main__ import app

if TYPE_CHECKING:
    import pytest

_IMPORT = "@.punt-labs/vox/CLAUDE.md"


def _point_repo_at(monkeypatch: pytest.MonkeyPatch, root: Path | None) -> None:
    def fake_root(start: Path | None = None) -> Path | None:
        return root

    monkeypatch.setattr("punt_vox.enablement.find_repo_root", fake_root)


def test_enable_writes_marker_and_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_repo_at(monkeypatch, tmp_path)
    result = CliRunner().invoke(app, ["enable"])
    assert result.exit_code == 0
    assert (tmp_path / ".punt-labs" / "vox" / "enabled").is_file()
    assert _IMPORT in (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")


def test_disable_removes_marker_leaves_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_repo_at(monkeypatch, tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["enable"])
    result = runner.invoke(app, ["disable"])
    assert result.exit_code == 0
    assert not (tmp_path / ".punt-labs" / "vox" / "enabled").is_file()
    assert _IMPORT not in (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    # Non-destructive: the dormant subtree survives.
    assert (tmp_path / ".punt-labs" / "vox").is_dir()


def test_disable_purge_removes_subtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_repo_at(monkeypatch, tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["enable"])
    result = runner.invoke(app, ["disable", "--purge"])
    assert result.exit_code == 0
    assert not (tmp_path / ".punt-labs" / "vox").is_dir()
    assert _IMPORT not in (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")


def test_enable_outside_a_repo_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _point_repo_at(monkeypatch, None)
    result = CliRunner().invoke(app, ["enable"])
    assert result.exit_code == 1
    assert "git repository" in result.output.lower()
