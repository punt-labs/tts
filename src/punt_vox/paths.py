"""Shared per-user path resolution for voxd and the vox CLI.

Both ``voxd.py`` and ``service.py`` need a consistent view of where the
daemon's per-user state lives. Previously each file defined its own
``_data_root()``/``_config_dir()``/``_log_dir()``/``_run_dir()`` helpers
that resolved to FHS system paths (``/etc/vox``, ``/var/log/vox``,
``/var/run/vox``) on Linux and Homebrew-prefix paths on macOS. That was a
regression: voxd runs as a single user (``User=`` in the systemd unit,
``UserName`` in the launchd plist), so its state is per-user, not
system-shared. The FHS paths stranded user API keys on upgrade, required
``sudo`` to edit personal tokens, and created a chown mismatch where the
file voxd was told to read was owned by root.

This module is the single source of truth for those paths. Keep it
lightweight — stdlib only — so both the heavy voxd import chain and the
minimal client can depend on it without pulling providers.
"""

from __future__ import annotations

import importlib.metadata
from pathlib import Path

_STATE_DIR_NAME = ".punt-labs"
_SUBDIR_NAME = "vox"


def user_state_dir() -> Path:
    """Return ``~/.punt-labs/vox`` for the current user.

    Same path on macOS and Linux. No FHS split, no Homebrew prefix.
    """
    return Path.home() / _STATE_DIR_NAME / _SUBDIR_NAME


def config_dir() -> Path:
    """Directory holding ``keys.env`` — same as the state dir root."""
    return user_state_dir()


def log_dir() -> Path:
    """Rotating log files live under ``<state>/logs``."""
    return user_state_dir() / "logs"


def run_dir() -> Path:
    """Runtime state (``serve.port``, ``serve.token``) under ``<state>/run``.

    This directory holds the auth token and is created with mode 0700
    so other local users cannot read it.
    """
    return user_state_dir() / "run"


def recordings_dir() -> Path:
    """Daemon-owned recordings store under ``<state>/recordings``.

    Every ``record`` write lands here under a name the daemon controls; the
    directory is created 0700 so no other local user can read a recording. It
    is the containment root for the path checks in
    :class:`~punt_vox.voxd.record_store.RecordStore` -- a wire client never
    names a path outside it.
    """
    return user_state_dir() / "recordings"


def keys_env_file() -> Path:
    """Full path to ``keys.env`` inside the config dir."""
    return config_dir() / "keys.env"


def ensure_user_dirs(state_root: Path | None = None) -> None:
    """Create the per-user state dir and its required subdirectories.

    Creates ``<state_root>``, ``<state_root>/logs``, ``<state_root>/run``,
    ``<state_root>/cache``, and ``<state_root>/recordings``. When *state_root*
    is ``None``, resolves to the current user's state dir via
    ``user_state_dir()``.

    All dirs are chmod 0700 because every one of them holds private
    per-user state: provider API keys, spoken-text logs, auth token,
    cached synthesis output, saved recordings. The chmod is applied on every call,
    not just at creation time, so pre-existing directories with looser
    permissions (for example 0755 from an older version that respected
    process umask) are tightened on the next startup.

    Idempotent: safe to call repeatedly. Does not chown — callers are
    expected to invoke this as the target user.
    """
    if state_root is None:
        state_root = user_state_dir()
    state_root.mkdir(parents=True, exist_ok=True)
    state_root.chmod(0o700)
    for subdir in ("logs", "run", "cache", "recordings"):
        d = state_root / subdir
        d.mkdir(parents=True, exist_ok=True)
        # Enforce 0700 even on pre-existing dirs with looser permissions.
        d.chmod(0o700)


def installed_version() -> str:
    """Return the installed ``punt-vox`` package version.

    Reads ``importlib.metadata.version("punt-vox")``. An uninstalled
    source tree — no distribution metadata to read — is a broken
    environment for anything that needs the version: fail fast rather
    than fall back to a literal, per the org's "no 0.0.0 fallback"
    version-reporting standard. Used by both the ``vox doctor``
    daemon-staleness check and by voxd at startup when populating the
    health response, so both always resolve the one true version.
    """
    try:
        return importlib.metadata.version("punt-vox")
    except importlib.metadata.PackageNotFoundError as exc:
        # PackageNotFoundError.__str__ hardcodes "No package metadata was
        # found for {args[0]}" -- args[0] is a package NAME, not free text,
        # so raising it with a sentence mangles the message. RuntimeError
        # has no such contract; the hint reaches vox doctor / voxd startup verbatim.
        msg = "punt-vox not installed as a package -- run 'uv tool install punt-vox'"
        raise RuntimeError(msg) from exc
