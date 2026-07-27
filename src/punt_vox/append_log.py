"""A multi-writer-safe append-only log sink: atomic ``O_APPEND``, 0600, rotation.

Generalizes the ``vibe_trace`` line-writer. Every :meth:`AtomicAppendLog.append`
escapes its text to exactly one physical line and writes it in a *single*
``os.write`` under ``O_APPEND``. POSIX guarantees an ``O_APPEND`` write seeks to
end and appends atomically, and a log line is far shorter than the platform's
atomic-append size, so lines from concurrent processes never interleave -- no
lock, no long-lived shared handle. Many writers therefore share one file safely.

The error path must never re-enter the logging subsystem: many concurrent
processes append here through ``logging``, so calling ``logger.*`` on an
``OSError`` would recurse straight back into this sink. On failure it writes a
best-effort note to ``sys.__stderr__`` and returns.

Rotation is safe under concurrent writers, guarded by a ``flock`` protocol on a
stable ``<path>.rotate.lock`` file (never itself renamed, so the lock identity
survives the rename chain). Every append holds ``LOCK_SH`` across
``open -> write -> close``; a rotation takes ``LOCK_EX`` -- which ``flock`` grants
only once every shared holder has drained -- and re-checks the size before
renaming. So no rename runs while any writer has an open append fd (no write to a
renamed file), and at most one rotator renames per threshold crossing (no double
rotate). This realizes the state model in ``docs/vox-2594-log-rotation.tex``.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Self, TypedDict, final

from punt_vox.log_sanitize import SANITIZER
from punt_vox.private_state import PrivateState
from punt_vox.voxd.data_root_boundary import relativize_to_data_root

__all__ = ["AtomicAppendLog", "SinkHealth"]


class SinkHealth(TypedDict):
    """The sink's client-observable status: a relative name and a verdict.

    ``name`` is relativized (``logs/vox.log``), never a host prefix; ``status``
    is ``"healthy"`` when the sink can append now, else ``"degraded"`` (a7dd).
    """

    name: str
    status: str


# ``O_NOFOLLOW`` refuses a symlink at the log path -- never legitimate, and a
# redirect-through-symlink vector.
_OPEN_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
# The stable rotate-lock file is opened read-write so it can carry both a shared
# (append) and an exclusive (rotate) flock; it is never renamed.
_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
_LOCK_SUFFIX = ".rotate.lock"
_LOCK_MODE = 0o600  # private per-user lock file
_MAX_BYTES = 5_242_880  # 5 MB
_BACKUP_COUNT = 5


@final
class AtomicAppendLog:
    """An append-only sink safe for many concurrent writers to one file."""

    __slots__ = ("_backup_count", "_guard", "_lock_path", "_max_bytes", "_path")

    _path: Path
    _lock_path: Path
    _guard: PrivateState
    _max_bytes: int
    _backup_count: int

    def __new__(
        cls,
        path: Path,
        *,
        max_bytes: int = _MAX_BYTES,
        backup_count: int = _BACKUP_COUNT,
    ) -> Self:
        self = super().__new__(cls)
        self._path = path
        self._lock_path = path.with_name(path.name + _LOCK_SUFFIX)
        # Route tighten failures to stderr, never logging: append runs inside a
        # logging emit, so a logged failure would recurse into this very sink.
        self._guard = PrivateState.for_append_sink(path)
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        return self

    @property
    def path(self) -> Path:
        """Return the file this sink appends to."""
        return self._path

    def health(self) -> SinkHealth:
        """Return the sink's relative name and a healthy/degraded verdict.

        The client-observable answer to "is this log itself working?" -- surfaced
        through ``mic:status`` so a broken sink is queryable, never buried in a
        second daemon log. The path crosses relativized (``logs/vox.log``) with
        the bare name as the fail-closed fallback, so no absolute prefix (home +
        username) reaches a client (the a7dd chroot); ``status`` is the verdict.
        """
        rel = relativize_to_data_root(self._path)
        name = str(rel.path) if rel is not None else self._path.name
        return {"name": name, "status": "healthy" if self.is_writable() else "degraded"}

    def append(self, text: str) -> None:
        """Escape *text* to one line and append it atomically; never raise, never log.

        *text* is escaped by the shared :data:`SANITIZER` so the record is exactly
        one physical line no control byte can forge or corrupt -- callers pass raw
        content and trust the sink to keep it one line. The ``encode`` uses
        ``backslashreplace`` and runs *inside* the guard, so a lone surrogate in
        *text* (which ``str.translate`` passes through) can never raise a
        ``UnicodeEncodeError`` out of this never-raise path. A short write (only
        ``ENOSPC`` in practice, since Python auto-retries ``EINTR``) is *not*
        looped: re-issuing the remainder would be a second append another writer's
        line could split, tearing the very line ``O_APPEND`` protects. On any
        ``OSError`` the sink writes to ``sys.__stderr__`` -- never through
        ``logging``, which would recurse into the client log handler this sink
        backstops.
        """
        try:
            line = f"{SANITIZER.escape(text)}\n".encode(errors="backslashreplace")
            self._guard.ensure_private_tree()
            self._append_guarded(line)
        except OSError as exc:
            self._to_stderr(f"cannot append to {self._path}: {exc}")

    def _append_guarded(self, line: bytes) -> None:
        """Append *line* under the rotate lock: shared to write, exclusive to rotate.

        Realizes the ``flock`` protocol modeled in
        ``docs/vox-2594-log-rotation.tex``. A rotation runs first (only when this
        line would cross ``max_bytes``) under ``LOCK_EX``; the write then runs
        under ``LOCK_SH`` held across ``open -> write -> close``. Because
        ``LOCK_EX`` cannot be granted until every ``LOCK_SH`` holder has released,
        no rename ever runs while a writer has an open append fd.
        """
        lock_fd = self._open_lock()
        try:
            if self._would_overflow(len(line)):
                self._rotate_locked(lock_fd, len(line))
            fcntl.flock(lock_fd, fcntl.LOCK_SH)
            try:
                self._write_line(line)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    def _open_lock(self) -> int:
        """Open the stable rotate-lock file 0600, refusing a symlink at its path.

        A dedicated lock file -- never itself rotated -- gives a lock identity that
        survives the rename chain, so a writer and a rotator serialize on the same
        inode across rotations. The ``fchmod`` re-tightens a pre-existing lock file
        left loose by an earlier, laxer run (best-effort, like the append fd).
        """
        fd = os.open(self._lock_path, _LOCK_FLAGS, _LOCK_MODE)
        with contextlib.suppress(OSError):
            os.fchmod(fd, _LOCK_MODE)
        return fd

    def _would_overflow(self, incoming: int) -> bool:
        """Return whether appending *incoming* bytes would cross ``max_bytes``.

        A missing file (``OSError`` from ``stat``) has zero size, so the first
        append never rotates. ``max_bytes <= 0`` disables rotation entirely.
        """
        if self._max_bytes <= 0:
            return False
        try:
            size = self._path.stat().st_size
        except OSError:
            return False
        return size + incoming > self._max_bytes

    def _rotate_locked(self, lock_fd: int, incoming: int) -> None:
        """Rotate under ``LOCK_EX``, re-checking so a peer's rotate is a no-op.

        ``LOCK_EX`` blocks until every shared holder has drained, so the rename
        runs with no writer mid-append. The size re-check under the exclusive lock
        is the idempotency that closes the double-rotate race: a rotator that
        queued behind another finds the fresh, small file and skips.
        """
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            if self._would_overflow(incoming):
                self._rotate()
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)

    def _write_line(self, line: bytes) -> None:
        """Append *line* through one ``O_APPEND`` fd; a short write goes to stderr.

        A short write (only ``ENOSPC`` in practice, since Python auto-retries
        ``EINTR``) is *not* looped: re-issuing the remainder would be a second
        append another writer's line could split, tearing the very line
        ``O_APPEND`` protects.
        """
        fd = self._guard.open_private(_OPEN_FLAGS)
        try:
            if (written := os.write(fd, line)) != len(line):
                self._to_stderr(
                    f"short append to {self._path}: {written} of {len(line)} bytes"
                )
        finally:
            os.close(fd)

    def is_writable(self) -> bool:
        """Return whether a line could be appended right now; never raise.

        Health is a property of the *paths*, not of any one process's last write:
        separate processes append here, so trusting a single writer's success
        would report a peer's state. The append path opens BOTH the rotate lock and
        the log file, so both must be writable-or-creatable -- a log the process can
        append to is still unusable if the lock it must first take cannot be created.
        """
        return self._can_write(self._path) and self._can_write(self._lock_path)

    @staticmethod
    def _can_write(path: Path) -> bool:
        """Return whether *path* is appendable or creatable right now; never raise.

        Probes with ``lstat`` so the answer matches the append open's
        ``O_NOFOLLOW``: a symlink at the final component -- live or dangling -- is
        rejected, because the open refuses it and every write would fail even though
        the link target may look writable. An existing regular file must be writable;
        a not-yet-created path needs its nearest existing ancestor to grant both
        write and search (``W_OK | X_OK``). Any :class:`OSError` from probing the
        real filesystem is fail-safe (report ``False``) so a health check can never
        crash its surface.
        """
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return AtomicAppendLog._anchor_writable(path)
        except OSError:
            return False
        if not stat.S_ISREG(mode):
            return False  # a symlink (O_NOFOLLOW refuses) or a non-regular entry
        return os.access(path, os.W_OK)

    @staticmethod
    def _anchor_writable(path: Path) -> bool:
        """Return whether *path*'s nearest existing ancestor grants create+search."""
        try:
            anchor = next(parent for parent in path.parents if parent.exists())
            return anchor.is_dir() and os.access(anchor, os.W_OK | os.X_OK)
        except OSError:
            return False

    def tighten_existing(self) -> tuple[Path, ...]:
        """Force the active file, every backup, and the lock to 0600; return failures.

        A startup sweep: a file left 0644 by an earlier, laxer run is re-tightened,
        and every path it could not force to 0600 is returned so a caller (the
        daemon config) can WARN about it durably once its handlers exist. Never
        raises -- a vanished slot is benign; a symlink or a genuine ``chmod`` refusal
        is a real failure and is reported.
        """
        return tuple(path for path in self._all_files() if not self._tighten_one(path))

    def _all_files(self) -> Iterator[Path]:
        """Yield the active file, every possible backup slot, and the rotate lock."""
        yield self._path
        for n in range(1, self._backup_count + 1):
            yield self._path.with_name(f"{self._path.name}.{n}")
        yield self._lock_path

    @staticmethod
    def _tighten_one(path: Path) -> bool:
        """Return whether *path* is now a private (0600) regular file; never raise.

        Opens the slot with ``O_NOFOLLOW`` and ``fchmod``s *that fd* -- never the
        path -- so a ``*.log.N -> victim`` symlink cannot redirect the chmod onto
        the victim. A vanished slot (``ENOENT``) is benign and reported tightened; a
        symlink, non-regular entry, or ``EPERM`` is a real failure.
        """
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return False
            os.fchmod(fd, _LOCK_MODE)
        except OSError:
            return False
        finally:
            os.close(fd)
        return True

    def _rotate(self) -> None:
        """Best-effort rename chain; a failure leaves the sink appending in place."""
        for n in range(self._backup_count, 0, -1):
            src = (
                self._path
                if n == 1
                else self._path.with_name(f"{self._path.name}.{n - 1}")
            )
            dst = self._path.with_name(f"{self._path.name}.{n}")
            try:
                if src.exists():
                    src.replace(dst)
            except OSError as exc:
                self._to_stderr(f"rotation stalled renaming {src}: {exc}")
                return

    @staticmethod
    def _to_stderr(message: str) -> None:
        """Write a best-effort note to the real stderr; swallow if unavailable."""
        err = sys.__stderr__
        if err is None:
            return
        with contextlib.suppress(OSError, ValueError):
            err.write(f"[append-log] {message}\n")
