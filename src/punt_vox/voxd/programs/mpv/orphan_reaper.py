"""Reap an mpv orphaned by an unclean prior daemon exit before a fresh spawn.

mpv is spawned ``start_new_session=True`` with ``--idle=yes``, so a SIGKILL,
OOM, or crash of the daemon *before* :meth:`MpvSupervisor._teardown` runs leaves
an idle-forever mpv holding our IPC socket. The next daemon start would then
spawn a *second* mpv, breaking the single-player invariant (I2) across restarts.

:class:`OrphanReaper` closes that gap. On spawn the supervisor records the live
pid via :meth:`record`; on a clean teardown it drops the record via :meth:`clear`.
At the next startup :meth:`reap` runs before the first spawn: if the recorded pid
still names a live process it is killed, and the stale socket is removed so the
fresh mpv binds cleanly. This is startup hygiene -- no new modeled state, just an
enforcement of I2 that survives an unclean exit.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import socket
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["OrphanReaper"]

logger = logging.getLogger(__name__)


@final
class OrphanReaper:
    """Enforce single-mpv (I2) across daemon restarts via a pidfile + socket probe."""

    __slots__ = ("_pidfile", "_socket")
    _socket: Path
    _pidfile: Path

    def __new__(cls, socket_path: Path) -> Self:
        self = super().__new__(cls)
        self._socket = socket_path
        self._pidfile = socket_path.with_name(socket_path.name + ".pid")
        return self

    def reap(self) -> None:
        """Kill a still-live orphan on our socket, then clear the stale socket/pidfile.

        The recorded pid is authoritative -- if it names a live process, it is the
        orphan and is killed. A socket that is still *listening* with no pid to
        name it is logged (we cannot safely identify the owner to kill), then its
        path is unlinked so the fresh mpv can bind a new inode there.
        """
        pid = self._recorded_pid()
        if pid is not None and self._is_live(pid):
            logger.warning("reaping orphaned mpv pid %d left by an unclean exit", pid)
            self._kill(pid)
        elif self._socket_is_listening():
            logger.warning(
                "mpv socket %s is live but unidentified; clearing the stale path",
                self._socket,
            )
        self._clear_socket()
        self._clear_pidfile()

    def record(self, pid: int) -> None:
        """Record the live mpv pid so a later startup can reap it if orphaned."""
        with contextlib.suppress(OSError):
            self._pidfile.write_text(str(pid))

    def clear(self) -> None:
        """Drop the pid record on a clean teardown -- there is no orphan to reap."""
        self._clear_pidfile()

    def _recorded_pid(self) -> int | None:
        # None is the documented "no pid recorded" contract (absence), not a
        # failure to produce one: an absent or unreadable pidfile means nothing
        # to reap, and a garbled record is ignored rather than trusted.
        try:
            text = self._pidfile.read_text()
        except OSError:
            return None
        try:
            return int(text.strip())
        except ValueError:
            return None

    @staticmethod
    def _is_live(pid: int) -> bool:
        """Return whether ``pid`` names a live process (signal 0 is a probe)."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists but is not ours to signal
        return True

    @staticmethod
    def _kill(pid: int) -> None:
        """SIGKILL ``pid``, ignoring a race where it already exited."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)

    def _socket_is_listening(self) -> bool:
        """Return whether a process is accepting on our IPC socket path."""
        if not self._socket.exists():
            return False
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(self._socket))
        except OSError:
            return False
        finally:
            probe.close()
        return True

    def _clear_socket(self) -> None:
        """Remove the stale socket path, ignoring an already-absent one."""
        with contextlib.suppress(FileNotFoundError):
            self._socket.unlink()

    def _clear_pidfile(self) -> None:
        """Remove the pid record, ignoring an already-absent one."""
        with contextlib.suppress(FileNotFoundError):
            self._pidfile.unlink()
