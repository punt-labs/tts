"""Reap an mpv orphaned by an unclean prior daemon exit before a fresh spawn.

mpv is spawned ``start_new_session=True`` with ``--idle=yes``, so a SIGKILL,
OOM, or crash of the daemon *before* :meth:`MpvSupervisor._teardown` runs leaves
an idle-forever mpv holding our IPC socket. The next daemon start would then
spawn a *second* mpv, breaking the single-player invariant (I2) across restarts.

:class:`OrphanReaper` closes that gap by the *socket*, not by a recorded pid.
Our mpv is the only process that can own our ``--input-ipc-server`` socket path,
so the socket is a safe identity: at the next startup :meth:`reap` runs before
the first spawn and, if the path is still listening, connects and sends mpv's
``quit`` command -- a clean shutdown of exactly the process that owns *our*
socket, with no pid guessing and no risk to any unrelated process. The stale
socket path is then unlinked so the fresh mpv binds a new inode.

There is deliberately no kill-by-pid fallback. A recorded pid survives a reboot
and can be recycled by an unrelated process; SIGKILLing it would kill the wrong
target. A wedged mpv that ignores ``quit`` is rare; it is logged and left, which
is safer than ever risking the wrong process. This is startup hygiene enforcing
I2 -- no new modeled state, just a clean bring-up after an unclean exit.
"""

from __future__ import annotations

import logging
import socket
from typing import TYPE_CHECKING, Self, final

from punt_vox.types_programs.mpv_event import MpvCommand

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["OrphanReaper"]

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_SECONDS = 2.0
"""Bound the connect/send probe so a wedged socket cannot stall bring-up."""

_REAP_REQUEST_ID = 0
"""The ``request_id`` on the fire-and-forget ``quit`` -- no reply is awaited."""


@final
class OrphanReaper:
    """Enforce single-mpv (I2) across daemon restarts via a socket-identity quit."""

    __slots__ = ("_socket",)
    _socket: Path

    def __new__(cls, socket_path: Path) -> Self:
        self = super().__new__(cls)
        self._socket = socket_path
        return self

    def reap(self) -> None:
        """Quit a responsive orphan on our socket, then unlink the stale path.

        The socket is the identity: only our mpv can own our IPC socket, so a
        listening path is quit cleanly over IPC -- never killed by pid. A path
        that exists but no longer listens (a stale inode) is simply unlinked so
        the fresh mpv can bind there. Every socket operation is robust to
        ``OSError``, so a probe or unlink failure is logged, not raised into the
        supervisor.
        """
        if self._quit_listening_owner():
            logger.warning(
                "quit an orphaned mpv holding %s left by an unclean exit",
                self._socket,
            )
        self._unlink_socket()

    def _quit_listening_owner(self) -> bool:
        """Connect to a listening socket and send ``quit``; return whether one answered.

        Returns ``True`` only when the socket was listening and the ``quit`` frame
        was sent -- an orphan owned our socket and was asked to exit cleanly. A
        missing socket, a stale inode with no listener, or any probe error means
        there is nothing to quit and returns ``False``.
        """
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(_PROBE_TIMEOUT_SECONDS)
        try:
            return self._connect_and_quit(probe)
        finally:
            probe.close()

    def _connect_and_quit(self, probe: socket.socket) -> bool:
        """Connect ``probe`` to our socket and send ``quit``; report success."""
        try:
            probe.connect(str(self._socket))
        except OSError:
            return False  # no socket, a stale inode, or unreachable -- nothing to quit
        try:
            probe.sendall(MpvCommand.quit().framed(_REAP_REQUEST_ID))
        except OSError as exc:
            logger.warning(
                "quitting the orphan mpv on %s failed: %s", self._socket, exc
            )
            return False
        return True

    def _unlink_socket(self) -> None:
        """Remove the stale socket path, robust to any ``OSError``, not just absence."""
        try:
            self._socket.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "could not unlink the stale mpv socket %s: %s", self._socket, exc
            )
