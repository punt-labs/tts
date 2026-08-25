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
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Self, cast, final

from punt_vox.atomic_file import AtomicFile
from punt_vox.dirs import DEFAULT_CONFIG_DIR, find_repo_root
from punt_vox.private_state import PrivateState
from punt_vox.types_programs.wire import JsonObject

logger = logging.getLogger(__name__)

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
    exclusivity. No custom ``__new__``: :class:`RuntimeError` already
    accepts and stores an arbitrary positional argument via ``args``, so
    the live holder's :class:`CallLockState` rides there directly --
    :attr:`state` reads it back, the same shape
    :class:`~.session_attach.BareAuthMissingError` uses for a plain string
    payload, generalised to a structured one.
    """

    @property
    def state(self) -> CallLockState:
        """Return the live holder's recorded reason and pid."""
        return cast("CallLockState", self.args[0])

    def __str__(self) -> str:
        return (
            f"a call is already active, pid {self.state.pid}: "
            f"{self.state.reason}; run `vox call stop` first"
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

    @classmethod
    def for_repo(cls) -> Self:
        """Build a :class:`CallLock` at this repo's ``call.lock`` path.

        ``vox call start/stop/transfer`` each rebuilt the same
        ``<config-dir>/call/call.lock`` path independently -- this is the
        single place that construction is written, mirroring
        :meth:`~punt_vox.session_spec.SessionSpec.for_repo`'s own precedent.
        """
        root = find_repo_root() or Path.cwd()
        return cls(root / DEFAULT_CONFIG_DIR / "call" / "call.lock")

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
        silently overwritten. Written via :class:`AtomicFile`, not a bare
        ``write_text``: a reader (``plugin/hooks/call-lock.sh``) polls this
        file every 20ms, and a truncate-then-write is a window in which it
        could read a partial, unparseable file.
        """
        existing = self.read()
        if existing is not None and self._process_is_alive(existing.pid):
            raise CallLockActiveError(existing)
        payload = {"reason": reason, "pid": os.getpid()}
        # Session ids reachable through call.control's transfer requests are
        # capability-like for --resume -- whoever can read this directory
        # can potentially attach to the call's session. 0o700 dir / 0o600
        # file, not the AtomicFile default (0o644 world-readable).
        PrivateState(self._path).ensure_private_tree()
        AtomicFile(self._path).replace(json.dumps(payload), mode=0o600)

    def release(self) -> None:
        """Remove the lock file if present; a no-op if it is already gone."""
        self._path.unlink(missing_ok=True)

    def read(self) -> CallLockState | None:
        """Return the current lock state, or ``None`` if no call is active.

        A file that exists but fails to parse or lacks the expected shape
        (truncated by a racing writer, hand-edited, corrupted) is treated
        the same as a missing one -- logged and reported as "no active
        call" -- rather than raising and taking down whatever boundary
        happens to be calling :meth:`acquire`.

        Does not check pid liveness -- a lock file can outlive the process
        that wrote it (``SIGKILL``, a killed foreground process). Callers
        that need to know whether a call is genuinely live, not merely
        "a lock file exists", must use :meth:`is_live` instead.
        """
        try:
            # Read and parse share one try (mirrors CallControl.consume):
            # AtomicFile.read's UnicodeDecodeError is a ValueError subclass,
            # so corrupt bytes discard the same way bad JSON does. OSError
            # (a permissions or disk I/O failure on the read) is the same
            # kind of "unusable entry" and must discard the same way too.
            raw = AtomicFile(self._path).read()
            if not raw:
                return None
            # JsonObject.require_int already excludes bool (an int
            # subclass) -- without that, a hand-edited ``"pid": true``
            # would coerce to PID 1 in os.kill, a real, foreign process
            # that always exists, producing a permanently stuck "call is
            # active" false positive with no diagnostic.
            obj = JsonObject.parse(raw, "call.lock")
            return CallLockState(
                reason=obj.require_str("reason"), pid=obj.require_int("pid")
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("call.lock is unreadable, treating as stale: %s", exc)
            return None

    def is_live(self) -> bool:
        """Return whether a call is genuinely live -- a lock file recording a pid
        that :meth:`_process_is_alive` still confirms, not merely a lock file
        that exists.

        A stale lock (the recorded pid is dead -- crashed, killed, or a
        process that exited without reaching :meth:`release`) must be
        treated identically to "no call is active": :meth:`acquire` already
        makes this distinction to decide whether to refuse or silently
        overwrite; callers that only ask "is a call running" (``vox call
        stop``/``transfer``) need the same answer, not just file existence.
        """
        state = self.read()
        return state is not None and self._process_is_alive(state.pid)

    @staticmethod
    def _process_is_alive(pid: int) -> bool:
        """Return whether *pid* names a live process, via a no-op signal probe.

        ``PermissionError`` (the pid exists but is owned by another user)
        still proves existence -- only ``ProcessLookupError`` means the pid
        is genuinely gone.
        """
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
