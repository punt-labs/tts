"""The UserPromptSubmit lock: block interactive human input while a call is live.

Per this mission's contract: a lock file, written when a call starts and
removed when it ends, that a Claude Code ``UserPromptSubmit`` hook checks
before letting the human's interactive prompt through. The call's own
turns never trip this lock -- ``plugin/hooks/call-lock.sh`` reads the
``VOX_CALL_RELAY`` environment variable, which is set only on the
subprocess ``vox call start`` itself spawns to relay a turn, and bypasses
the block when it is present.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Self, final

__all__ = ["CallLock", "CallLockState"]


@final
@dataclass(frozen=True, slots=True)
class CallLockState:
    """What a call recorded in its lock file: why it is holding the lock, and who."""

    reason: str
    pid: int


@final
class CallLock:
    """A file-based lock one active call holds for its lifetime.

    Not a general-purpose concurrency primitive -- one call process
    acquires it at start and releases it at end; the file's mere presence
    is the signal ``plugin/hooks/call-lock.sh`` reads. Corresponds to the
    contract's "a lock file voxd owns": in this slice, the ``vox call
    start`` process that owns the call's lifecycle also owns this file,
    since daemon-side call orchestration is deferred to the mic:call MCP
    tool follow-up (``src/punt_vox/commands/call.py``'s module docstring).
    """

    __slots__ = ("_path",)
    _path: Path

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        self._path = path
        return self

    @property
    def path(self) -> Path:
        """Return the lock file's path."""
        return self._path

    def acquire(self, reason: str) -> None:
        """Write the lock file, recording *reason* and this process's pid."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"reason": reason, "pid": os.getpid()}
        self._path.write_text(json.dumps(payload))

    def release(self) -> None:
        """Remove the lock file if present; a no-op if it is already gone."""
        self._path.unlink(missing_ok=True)

    def read(self) -> CallLockState | None:
        """Return the current lock state, or ``None`` if no call is active."""
        try:
            raw = self._path.read_text()
        except FileNotFoundError:
            return None
        payload = json.loads(raw)
        return CallLockState(reason=payload["reason"], pid=payload["pid"])
