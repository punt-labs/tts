"""Read/write access to a single vox config file's YAML frontmatter."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Self, final

from punt_vox.frontmatter_block import FrontmatterBlock

logger = logging.getLogger(__name__)

__all__ = ["Frontmatter"]

# Expressive mood text: log its length, never the content (PY-CS-11 privacy).
_REDACTED_KEYS = frozenset({"vibe", "vibe_tags"})


@final
class Frontmatter:
    """Owns one config file and reads/writes its YAML frontmatter fields."""

    __slots__ = ("_path",)

    _path: Path

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        self._path = path
        return self

    @property
    def path(self) -> Path:
        """Return the backing file path."""
        return self._path

    @staticmethod
    def validate_value(value: str) -> None:
        """Reject values that would corrupt the ``key: "<value>"`` round-trip.

        Raises :class:`~punt_vox.types_errors.ConfigValueError`. Kept on this
        class because the config layer validates before routing a batch to
        two files, and asks the writer, not the format, whether a value fits.
        """
        FrontmatterBlock.validate_value(value)

    def read_fields(self) -> dict[str, str]:
        """Return all non-empty frontmatter fields, or ``{}`` if unreadable."""
        block = self._block()
        return {} if block is None else block.fields()

    def read_field(self, field: str) -> str | None:
        """Return a single frontmatter field, or ``None`` if absent/unreadable."""
        block = self._block()
        return None if block is None else block.field(field)

    def write_field(self, key: str, value: str) -> None:
        """Write a single key-value pair into the frontmatter."""
        self.write_fields({key: value})

    def write_fields(self, updates: dict[str, str]) -> None:
        """Write multiple key-value pairs in a single read-write cycle.

        Every value is validated up front, so the serialization invariant
        is enforced by the class that serializes -- a caller bypassing
        ``ConfigStore`` still cannot corrupt the frontmatter (PY-EH-1).
        """
        for value in updates.values():
            FrontmatterBlock.validate_value(value)
        self._path.parent.mkdir(parents=True, exist_ok=True)

        if not self._path.exists():
            self._path.write_text(
                FrontmatterBlock.rendered(updates).text, encoding="utf-8"
            )
            return

        # Read strictly here, unlike ``_block``: a file that exists but will
        # not open is a fault, and rewriting it whole from *updates* alone
        # would destroy the fields this write was supposed to preserve.
        held = FrontmatterBlock(self._path.read_text(encoding="utf-8"))
        if held.accepts(updates):
            written = held.with_fields(updates)
        else:
            logger.warning("Malformed config (no closing ---): %s", self._path)
            written = FrontmatterBlock.rendered(updates)

        self._path.write_text(written.text, encoding="utf-8")
        self._log_write(updates)

    def _log_write(self, updates: dict[str, str]) -> None:
        """Log one INFO summary per write, with the per-field detail at DEBUG.

        A multi-field change (a vibe set writes several) reads as a single
        line, not one per field; the mood keys log their length so the
        expressive text never lands in a durable log.
        """
        for key, value in updates.items():
            shown = f"<{len(value)} chars>" if key in _REDACTED_KEYS else repr(value)
            logger.debug("Config: set %s = %s in %s", key, shown, self._path)
        logger.info("config: updated %d field(s) in %s", len(updates), self._path.name)

    def _block(self) -> FrontmatterBlock | None:
        """Return the file's frontmatter, or ``None`` when it cannot be read.

        ``None`` is the documented contract for "there is nothing on disk to
        read": symmetric with the write path, an absent or unreadable config
        (permissions or IO fault) degrades to defaults rather than crashing
        the hook subprocess (PY-EH-1).
        """
        if not self._path.exists():
            return None
        try:
            return FrontmatterBlock(self._path.read_text(encoding="utf-8"))
        except OSError as exc:
            logger.warning(
                "Config unreadable, using defaults: %s (%s)", self._path, exc
            )
            return None
