"""Drive the installed ``vox`` binary as a subprocess.

The rest of the suite imports ``punt_vox`` from the working tree, so none of it
can see a packaging fault: an entry point that does not resolve, a data file
left out of the wheel, a subcommand that exists in source but never reaches the
installed CLI. These tests run the binary that ``make install`` put on
``PATH`` -- the artifact a user actually gets.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

_TIMEOUT = 60.0

# The core subcommands the packaged CLI MUST expose. A missing entry here means
# either the entry point in pyproject.toml stopped resolving or a module was
# dropped from the wheel; the source-tree tests cannot see either.
_REQUIRED_COMMANDS = ("version", "status", "doctor", "say", "mcp")


def _vox() -> str:
    """Return the installed ``vox`` executable, skipping if absent."""
    binary = shutil.which("vox")
    if binary is None:
        pytest.skip("vox is not installed -- run `make install` first")
    return binary


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the installed CLI with ``args`` and capture its output."""
    return subprocess.run(
        [_vox(), *args],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
        check=False,
        cwd=cwd,
    )


def test_version_reports_the_installed_package() -> None:
    result = _run("version")

    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    assert "vox" in combined.lower()


def test_help_lists_core_subcommands() -> None:
    """A subcommand present in source but absent from the installed CLI is a
    packaging fault -- typically a module missing from the wheel."""
    result = _run("--help")

    assert result.returncode == 0, result.stderr
    missing = [c for c in _REQUIRED_COMMANDS if c not in result.stdout]
    assert not missing, f"subcommands absent from the installed CLI: {missing}"


def test_doctor_runs_from_an_unrelated_directory(tmp_path: Path) -> None:
    """``doctor`` must not depend on being launched from the source tree -- a
    consumer runs it from wherever they happen to be."""
    result = _run("doctor", cwd=tmp_path)

    combined = result.stdout + result.stderr
    assert "Traceback" not in combined, combined


def test_status_runs_from_an_unrelated_directory(tmp_path: Path) -> None:
    """``status`` reads config; must not crash without a repo-rooted cwd."""
    result = _run("status", cwd=tmp_path)

    combined = result.stdout + result.stderr
    assert "Traceback" not in combined, combined
