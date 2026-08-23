"""Drive the installed ``vox`` binary as a subprocess.

The rest of the suite imports ``punt_vox`` from the working tree, so none of it
sees a packaging fault: an entry point that does not resolve, a data file left
out of the wheel, a subcommand that exists in source but never reaches the
installed CLI. These tests run the ``vox`` binary that ``make install`` placed
in ``$HOME/.local/bin`` -- the artifact a user actually gets.

Binary resolution is EXPLICIT, not via ``shutil.which("vox")``: ``uv run
pytest`` prepends ``.venv/bin`` to ``PATH``, and after ``uv sync`` the
``.venv/bin/vox`` script points back at working-tree source. ``shutil.which``
would resolve to THAT and every packaging-fault check would silently pass on
the source tree. See ``test_e2e_binary_is_installed_wheel_not_editable``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

_TIMEOUT = 60.0

_REQUIRED_COMMANDS = ("version", "status", "doctor", "say", "mcp")


def _vox() -> Path:
    """Return the ``uv tool``-installed ``vox`` binary path.

    Override with ``VOX_E2E_BINARY`` when the tool bin dir is non-default.
    Skips (does not fail) when the binary is absent so the suite is usable
    on a workstation that has not run ``make install`` yet.
    """
    override = os.environ.get("VOX_E2E_BINARY")
    candidate = Path(override) if override else Path.home() / ".local" / "bin" / "vox"
    if not candidate.is_file():
        pytest.skip(
            f"installed vox not found at {candidate}; run `make install` "
            "(or set VOX_E2E_BINARY to the uv tool bin path)"
        )
    return candidate


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the installed CLI with ``args`` and capture its output."""
    return subprocess.run(
        [str(_vox()), *args],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
        check=False,
        cwd=cwd,
    )


def test_e2e_binary_is_installed_wheel_not_editable() -> None:
    """The resolved binary must be from the installed wheel, not the venv.

    ``uv run`` prepends ``.venv/bin`` to ``PATH``; a ``shutil.which("vox")``
    call from inside ``uv run pytest`` would return the editable working-tree
    shim and defeat the entire purpose of the packaging tier. The other tests
    use explicit path resolution; this test makes the invariant fail LOUDLY
    if a future change relaxes it.
    """
    binary = _vox()
    parts = binary.resolve().parts
    assert ".venv" not in parts, (
        f"e2e binary {binary} resolves inside .venv -- the wheel install is "
        "being bypassed and packaging faults would silently pass"
    )


def test_version_reports_the_installed_package() -> None:
    result = _run("version")

    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    assert "vox" in combined.lower()


@pytest.mark.parametrize("subcommand", _REQUIRED_COMMANDS)
def test_subcommand_help_resolves(subcommand: str) -> None:
    """Every required subcommand must render its own ``--help`` cleanly.

    Rich soft-wraps the top-level ``--help`` output at the runner's tty
    width, so scraping the wrapped table for command names is width-fragile
    (the same trap ``tests/_cli_introspect.py`` documents for the option-name
    column). Invoking ``vox <subcommand> --help`` and asserting exit 0 proves
    the subcommand is registered and reachable in the installed wheel with
    no substring match on rendered output.
    """
    result = _run(subcommand, "--help")

    assert result.returncode == 0, (
        f"`vox {subcommand} --help` failed: rc={result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


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
