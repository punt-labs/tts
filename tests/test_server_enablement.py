"""Tests for the ``mic`` enablement tool and CLI/MCP marker parity."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from punt_vox.__main__ import app
from punt_vox.enablement import RepoEnablement
from punt_vox.server_enablement import EnablementTool

if TYPE_CHECKING:
    import pytest

_IMPORT = "@.punt-labs/vox/CLAUDE.md"


def _tool(repo: Path) -> EnablementTool:
    return EnablementTool(lambda: RepoEnablement.for_repo(repo))


def _marker(repo: Path) -> Path:
    return repo / ".punt-labs" / "vox" / "enabled"


def test_enable_action_writes_marker_and_import(tmp_path: Path) -> None:
    reply = json.loads(_tool(tmp_path).dispatch("enable"))
    assert reply["action"] == "enable"
    assert reply["enabled"] is True
    assert reply["repo"] == str(tmp_path)
    assert _marker(tmp_path).is_file()
    assert _IMPORT in (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")


def test_disable_action_removes_marker_and_import(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    tool.dispatch("enable")
    reply = json.loads(tool.dispatch("disable"))
    assert reply["action"] == "disable"
    assert reply["enabled"] is False
    assert not _marker(tmp_path).is_file()
    assert _IMPORT not in (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")


def test_invalid_action_is_a_clean_error_object(tmp_path: Path) -> None:
    # The bool shape is retired (§2.14); an unknown action returns an error object,
    # never raises across the tool boundary.
    reply = json.loads(_tool(tmp_path).dispatch("true"))  # type: ignore[arg-type]
    assert "error" in reply
    assert not _marker(tmp_path).is_file()


def test_outside_a_repo_is_a_clean_error_object() -> None:
    def raiser() -> RepoEnablement:
        raise ValueError("not inside a git repository")

    reply = json.loads(EnablementTool(raiser).dispatch("enable"))
    assert "error" in reply
    assert "git repository" in reply["error"]


def test_cli_and_mcp_write_byte_identical_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One marker, two doors (§2.14): the CLI `vox enable` and the MCP enablement
    # action must produce byte-identical marker files.
    cli_repo = tmp_path / "cli"
    mcp_repo = tmp_path / "mcp"
    cli_repo.mkdir()
    mcp_repo.mkdir()

    def fake_root(start: Path | None = None) -> Path | None:
        return cli_repo

    monkeypatch.setattr("punt_vox.enablement.find_repo_root", fake_root)
    CliRunner().invoke(app, ["enable"])
    _tool(mcp_repo).dispatch("enable")

    assert _marker(cli_repo).read_bytes() == _marker(mcp_repo).read_bytes()
