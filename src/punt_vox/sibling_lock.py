"""Serialize a read-modify-write with an exclusive lock on a sibling lock file."""

from __future__ import annotations

import contextlib
import fcntl
import time
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path
    from typing import IO

__all__ = ["SiblingLock"]


@final
class SiblingLock:
    """An exclusive ``flock`` on a sibling of a host file, held across a whole RMW.

    ``tool-enable-disable.md`` § 2.4 mandates locking a **sibling**, never the
    host itself: the atomic rename that replaces the host swaps its inode, so a
    lock held on the host travels with the dead inode and the next writer
    serializes against nothing. The lock name is **tool-agnostic**
    (``.<host>.punt-import.lock``) so every punt CLI mutating the same host file
    takes the identical lock -- a per-tool name would serialize a tool only
    against itself and leave the cross-tool lost update in place.

    Acquisition is **bounded**: a blocking ``LOCK_EX`` would hang the CLI/MCP
    forever if a peer wedged the lock, so instead poll non-blocking a fixed
    number of times and raise :class:`TimeoutError` once the window elapses.
    """

    __slots__ = ("_host", "_path")

    _host: Path
    _path: Path

    # ~5s total: an honest RMW holds the lock for milliseconds, so anything past
    # this window is a wedged peer, not normal contention.
    _ACQUIRE_ATTEMPTS = 50
    _ACQUIRE_INTERVAL_S = 0.1

    def __new__(cls, host_path: Path) -> Self:
        self = super().__new__(cls)
        self._host = host_path
        self._path = host_path.parent / f".{host_path.name}.punt-import.lock"
        return self

    @property
    def path(self) -> Path:
        """Return the sibling lock file path."""
        return self._path

    @contextlib.contextmanager
    def held(self) -> Generator[None]:
        """Hold the exclusive lock for the duration of the ``with`` block."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as lock:
            self._acquire(lock)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _acquire(self, lock: IO[str]) -> None:
        """Take the exclusive lock, retrying on contention, then failing loud.

        Only ``BlockingIOError`` (EAGAIN/EWOULDBLOCK) is contention worth a
        bounded retry; every other ``OSError`` (ENOLCK, an ``flock``-less
        filesystem, EBADF) propagates at once, never a false ``TimeoutError``.
        """
        for _ in range(self._ACQUIRE_ATTEMPTS):
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                time.sleep(self._ACQUIRE_INTERVAL_S)
            else:
                return
        msg = f"another vox/punt process is writing {self._host}; retry"
        raise TimeoutError(msg)
