"""Provider API key management for the vox daemon.

The daemon runs as a launchd/systemd service with a stripped environment --
no API keys. This module manages ``~/.punt-labs/vox/keys.env``, a simple
``KEY=VALUE`` file written at ``vox daemon install`` time from the caller's
environment and loaded at daemon startup before any provider is instantiated.

The set of variable names ``vox daemon install`` snapshots into ``keys.env``
comes from :data:`punt_vox.providers.credentials.PROVIDER_KEY_NAMES`, which
in turn is derived from the same :class:`ProviderCredentials` dispatch that
answers the readiness gate. One source of truth, so the write side cannot
save a variable the gate does not read (or omit one it does).
"""

from __future__ import annotations

import logging
from pathlib import Path

from punt_vox.paths import user_state_dir
from punt_vox.providers.credentials import PROVIDER_KEY_NAMES

logger = logging.getLogger(__name__)

__all__ = [
    "PROVIDER_KEY_NAMES",
    "format_keys_env",
    "keys_file_path",
    "parse_keys_env",
]

_KEYS_FILE = user_state_dir() / "keys.env"


def keys_file_path() -> Path:
    """Return the path to the keys.env file."""
    return _KEYS_FILE


def parse_keys_env(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines.  Skip comments and blank lines."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if key:
            result[key] = value
    return result


def format_keys_env(keys: dict[str, str]) -> str:
    """Format as KEY=VALUE lines, sorted, with header comment."""
    header = (
        "# vox provider keys — loaded by daemon at startup\n"
        "# Written by: vox daemon install\n\n"
    )
    lines = [f"{k}={v}" for k, v in sorted(keys.items()) if v]
    return header + "\n".join(lines) + "\n"
