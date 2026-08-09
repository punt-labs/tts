"""Guarded refresh+write for the switch tools' config-side operations.

The MCP switch tools (``mic:model``, ``mic:provider``, ``mic:voice``) all
share the same config-side dependency: read the current values
(``session.refresh_from_config()``) and, when the caller named a value,
persist it (``ConfigStore(...).write_field(...)``). Both calls can raise
:class:`OSError` (unwritable disk, permission denied) or :class:`ValueError`
(malformed ``vox.md``, rejected value). The switch tools' contract is that
they *never* raise across their MCP boundary -- every fault becomes a JSON
``{"error": ...}`` envelope -- so the config calls need the same guarding
on every tool.

Holding the guard here (rather than duplicating three try/except pairs in
``server_switches.py``) means the invariant lives in one place; each switch
tool composes a :class:`ConfigWriter` at construction and calls
``self._config.refresh(...)`` / ``self._config.write(...)`` on its
config-side paths.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Final, Self, final

from punt_vox.config import ConfigStore

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from punt_vox.server import SessionConfig

__all__ = ["ConfigWriter"]

logger = logging.getLogger(__name__)

# Config-side faults on refresh and write paths. Filesystem trouble (disk
# full, permission denied) surfaces as OSError; a malformed vox.md file or
# a rejected value surfaces as ValueError. Both funnel to the same JSON
# error envelope so the tools' "never raise across the boundary" invariant
# holds on every path.
_CONFIG_IO_ERRORS: Final = (OSError, ValueError)


@final
class ConfigWriter:
    """Refresh a session or persist a field; return an error envelope on I/O faults."""

    __slots__ = ("_config_dir_finder",)
    _config_dir_finder: Callable[[], Path | None]

    def __new__(cls, config_dir_finder: Callable[[], Path | None]) -> Self:
        self = super().__new__(cls)
        self._config_dir_finder = config_dir_finder
        return self

    def refresh(self, session: SessionConfig) -> str | None:
        """Refresh *session* from disk; return an error envelope, or None on success."""
        return self._guarded(session.refresh_from_config)

    def write(self, field: str, value: str) -> str | None:
        """Persist *field* to disk; return an error envelope, or None on success."""
        return self._guarded(
            lambda: ConfigStore(self._config_dir_finder()).write_field(field, value)
        )

    def write_fields(self, updates: dict[str, str]) -> str | None:
        """Persist *updates* in one write; return an error envelope, or None."""
        return self._guarded(
            lambda: ConfigStore(self._config_dir_finder()).write_fields(updates)
        )

    @staticmethod
    def _guarded(action: Callable[[], None]) -> str | None:
        """Run *action*, funnelling filesystem/config faults to a JSON envelope."""
        try:
            action()
        except _CONFIG_IO_ERRORS as exc:
            logger.exception("Config I/O failed")
            return json.dumps({"error": str(exc)})
        return None
