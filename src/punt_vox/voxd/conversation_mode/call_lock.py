"""The UserPromptSubmit lock: block interactive human input while a call is live.

A lock file, written when a call starts and removed when it ends, that a
Claude Code ``UserPromptSubmit`` hook checks before letting the human's
interactive prompt through. The call's own turns never trip this lock --
``plugin/hooks/call-lock.sh`` reads the ``VOX_CALL_RELAY`` environment
variable, which is set only on the subprocess the call's session-attach
spawns to relay a turn, and bypasses the block when it is present.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Self, final

__all__ = ["CallLock", "CallLockActiveError", "CallLockState"]


@final
@dataclass(frozen=True, slots=True)
class CallLockState:
    """What a call recorded in its lock file: why it is holding the lock, and who."""

    reason: str
    pid: int


class CallLockActiveError(RuntimeError):
    """Raised by :meth:`CallLock.acquire` when a live process already holds the lock.

    A boundary error (PY-EH-1): a second ``vox call start`` refuses to
    clobber a genuinely active call rather than silently overwriting its
    lock file -- two processes racing to speak through the same
    :class:`~.call_lock.CallLock` would otherwise both believe they hold
    exclusivity. ``__str__`` is the load-bearing override :mod:`.stt_provider`
    and sibling typed errors already establish: :class:`BaseException`
    reinitialises ``args`` with the constructor's positional arguments after
    ``__new__`` runs, so a message stashed only there would round-trip out as
    a tuple repr instead of the caller-facing sentence.
    """

    __slots__ = ("_state",)
    _state: CallLockState

    def __new__(cls, state: CallLockState) -> Self:  # pyright: ignore[reportInconsistentConstructor]
        self = super().__new__(cls, state)
        self._state = state
        return self

    @property
    def state(self) -> CallLockState:
        """Return the live holder's recorded reason and pid."""
        return self._state

    def __str__(self) -> str:
        return (
            f"a call is already active, pid {self._state.pid}: "
            f"{self._state.reason}; run `vox call stop` first"
        )


@final
class CallLock:
    """A file-based lock one active call holds for its lifetime.

    Not a general-purpose concurrency primitive -- one call process
    acquires it at start and releases it at end; the file's mere presence
    is the signal ``plugin/hooks/call-lock.sh`` reads. The ``vox call
    start`` process that owns the call's lifecycle also owns this file.
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
        """Write the lock file, recording *reason* and this process's pid.

        Raises :class:`CallLockActiveError` when a *live* process already
        holds the lock -- checked with ``os.kill(pid, 0)``, an existence
        probe that delivers no signal, rather than trusting the file's mere
        presence. A crash (``SIGKILL``, terminal close) skips this class's
        own :meth:`release` and leaves a stale file behind; without the
        liveness check, that stale file would wedge every future call
        indefinitely. A lock whose recorded holder is gone is stale and is
        silently overwritten.
        """
        existing = self.read()
        if existing is not None and self._process_is_alive(existing.pid):
            raise CallLockActiveError(existing)
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

    @staticmethod
    def _process_is_alive(pid: int) -> bool:
        """Return whether *pid* names a live process, via a no-op signal probe."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
