"""Tests for punt_vox.paths -- shared path resolution for user state.

voxd and service both need the same view of where per-user state lives.
These tests pin the contract: everything under ``~/.punt-labs/vox/``, no
per-OS splits, no FHS system paths.
"""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import importlib.metadata
import re
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from punt_vox.paths import (
    config_dir,
    ensure_user_dirs,
    installed_version,
    keys_env_file,
    log_dir,
    run_dir,
    user_state_dir,
)

# ---------------------------------------------------------------------------
# Basic path layout
# ---------------------------------------------------------------------------


def test_user_state_dir_under_home() -> None:
    """user_state_dir is always ~/.punt-labs/vox/ -- no per-OS split."""
    assert user_state_dir() == Path.home() / ".punt-labs" / "vox"


def test_config_dir_is_state_dir() -> None:
    """keys.env lives directly in the state dir, not a subdir."""
    assert config_dir() == user_state_dir()


def test_log_dir_under_state() -> None:
    assert log_dir() == user_state_dir() / "logs"


def test_run_dir_under_state() -> None:
    assert run_dir() == user_state_dir() / "run"


def test_keys_env_file_in_config_dir() -> None:
    assert keys_env_file() == config_dir() / "keys.env"


def test_no_fhs_paths_leak_into_helpers() -> None:
    """None of the helpers may return /etc, /var, or brew prefix paths."""
    forbidden_prefixes = ("/etc/", "/var/", "/opt/homebrew/etc", "/usr/local/etc")
    for helper in (user_state_dir, config_dir, log_dir, run_dir):
        resolved = str(helper())
        for prefix in forbidden_prefixes:
            assert not resolved.startswith(prefix), (
                f"{helper.__name__} returned forbidden path {resolved}"
            )


# ---------------------------------------------------------------------------
# ensure_user_dirs -- directory creation + mode permissions
# ---------------------------------------------------------------------------


def test_ensure_user_dirs_creates_all_subdirs(tmp_path: Path) -> None:
    """All four subdirs are created under the target state dir."""
    state = tmp_path / "state"
    ensure_user_dirs(state)
    assert state.is_dir()
    assert (state / "logs").is_dir()
    assert (state / "run").is_dir()
    assert (state / "cache").is_dir()


def test_ensure_user_dirs_sets_all_subdirs_mode_0700(tmp_path: Path) -> None:
    """Every state subdir is mode 0700 — same policy as ``~/.ssh``.

    Every directory holds private per-user data: keys, spoken-text
    logs, auth token, cached audio. Tighten all of them, not just run.
    """
    state = tmp_path / "state"
    ensure_user_dirs(state)
    for name in ("", "logs", "run", "cache"):
        target = state / name if name else state
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o700, f"{target} mode is {oct(mode)}, expected 0o700"


def test_ensure_user_dirs_is_idempotent(tmp_path: Path) -> None:
    """Running twice does not crash and does not lower permissions."""
    state = tmp_path / "state"
    ensure_user_dirs(state)
    ensure_user_dirs(state)
    assert (state / "run").is_dir()
    for name in ("", "logs", "run", "cache"):
        target = state / name if name else state
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o700


# ---------------------------------------------------------------------------
# installed_version -- shared version resolution for doctor and voxd
# ---------------------------------------------------------------------------


def test_installed_version_returns_string() -> None:
    """Returns the currently installed punt-vox version as a semver string.

    The exact value changes on every release, so assert the shape
    rather than the literal string.
    """
    result = installed_version()
    assert isinstance(result, str)
    assert re.match(r"^\d+\.\d+\.\d+", result), (
        f"expected semver-prefixed string, got {result!r}"
    )


def test_installed_version_raises_on_missing_metadata() -> None:
    """No package metadata means no fallback -- fail fast, don't guess.

    An uninstalled source tree has no distribution to report a version
    for. Per the org's version-reporting standard, that is a broken
    environment: raise ``PackageNotFoundError`` rather than falling
    back to a literal that could silently drift from ``pyproject.toml``.
    """
    with (
        patch.object(
            importlib.metadata,
            "version",
            side_effect=importlib.metadata.PackageNotFoundError("punt-vox"),
        ),
        pytest.raises(importlib.metadata.PackageNotFoundError),
    ):
        installed_version()


def test_punt_vox_version_matches_installed_metadata() -> None:
    """``punt_vox.__version__`` is read from installed metadata, not a literal.

    Guards against the v5.0.1 regression: a release bumped
    ``pyproject.toml`` but a hardcoded ``__version__`` string in
    ``__init__.py`` was left stale. Asserting the two always agree
    makes that class of drift impossible.
    """
    import punt_vox

    assert punt_vox.__version__ == importlib.metadata.version("punt-vox")
