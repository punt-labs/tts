"""Additively register vox's repo-scoped permission entries in settings.json.

``tool-enable-disable.md`` § 2.8 lets ``enable`` touch one file outside vox's own
subtree -- ``<repo>/.claude/settings.json`` -- and only additively. The
deterministic entry set (``Bash(vox:*)``) is vox's identity in that file: a
``--no-plugin`` agent that a repo enabled needs the shell permission to drive the
``vox`` CLI. :class:`SettingsRegistration` adds the missing members with the
order-preserving merge of [permissions.md § 6] and removes them by exact
value-match, both under the shared sibling lock (``.settings.json.punt-import.lock``)
because the file is read-modified-written by other tools and invocations too.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Self, cast, final

from punt_vox.atomic_file import AtomicFile
from punt_vox.sibling_lock import SiblingLock

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["SettingsRegistration"]


@final
class SettingsRegistration:
    """Register or deregister a deterministic permission-entry set in a settings file.

    Bind the settings path and the entry set at construction. :meth:`register`
    appends the missing entries to ``permissions.allow`` preserving order;
    :meth:`deregister` removes every exact match. Both take the sibling lock for
    the whole read-modify-write, so a parallel ``enable`` never loses an update.
    """

    __slots__ = ("_entries", "_file", "_lock")

    _file: AtomicFile
    _lock: SiblingLock
    _entries: tuple[str, ...]

    _DEFAULT_ENTRIES: tuple[str, ...] = ("Bash(vox:*)",)

    def __new__(
        cls, settings_path: Path, entries: tuple[str, ...] = _DEFAULT_ENTRIES
    ) -> Self:
        self = super().__new__(cls)
        self._file = AtomicFile(settings_path)
        self._lock = SiblingLock(settings_path)
        self._entries = entries
        return self

    @property
    def entries(self) -> tuple[str, ...]:
        """Return the deterministic entry set this instance owns."""
        return self._entries

    def register(self) -> bool:
        """Add each entry to ``permissions.allow`` if absent. Return whether changed.

        Order-preserving and idempotent: an entry already present is skipped, so a
        re-run of ``enable`` adds nothing and rewrites nothing.
        """
        with self._lock.held():
            data = self._load()
            allow = self._existing_allow(data)
            missing = [entry for entry in self._entries if entry not in allow]
            if not missing:
                return False
            self._store_allow(data, allow + missing)
            return True

    def deregister(self) -> bool:
        """Remove every owned entry from ``permissions.allow``. Return whether changed.

        Exact value-match removal -- the identity is the entry set itself, so no tag
        schema is needed. Unrelated entries are left untouched.
        """
        with self._lock.held():
            data = self._load()
            allow = self._existing_allow(data)
            kept = [entry for entry in allow if entry not in self._entries]
            if len(kept) == len(allow):
                return False
            self._store_allow(data, kept)
            return True

    def _load(self) -> dict[str, Any]:
        """Return the parsed settings object, or ``{}`` when the file is empty/absent.

        A malformed existing file is a boundary error (PY-EH-1/PY-EH-8): raise
        rather than silently discard a user's settings, which resetting to ``{}``
        would do. ``dict[str, Any]`` is the JSON wire boundary -- the shape is the
        user's whole settings file, narrowed only where this class writes.
        """
        text = self._file.read()
        if not text.strip():
            return {}
        # The wire boundary: JSON deserialization yields object until narrowed.
        parsed: object = json.loads(text)
        if not isinstance(parsed, dict):
            msg = f"{self._file.path}: settings root must be a JSON object"
            raise ValueError(msg)
        return cast("dict[str, Any]", parsed)

    def _store_allow(self, data: dict[str, Any], allow: list[str]) -> None:
        """Write *allow* back into ``data.permissions.allow`` and replace the file."""
        perms: Any = data.get("permissions")
        if not isinstance(perms, dict):
            perms = {}
            data["permissions"] = perms
        cast("dict[str, Any]", perms)["allow"] = allow
        self._file.replace(json.dumps(data, indent=2) + "\n")

    @staticmethod
    def _existing_allow(data: dict[str, Any]) -> list[str]:
        """Return the current ``permissions.allow`` entries as strings, else ``[]``."""
        perms: Any = data.get("permissions")
        if not isinstance(perms, dict):
            return []
        allow: Any = cast("dict[str, Any]", perms).get("allow")
        if not isinstance(allow, list):
            return []
        return [str(entry) for entry in cast("list[object]", allow)]
