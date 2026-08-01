"""Tests for :class:`OrphanReaper` -- reaping an mpv left by an unclean exit.

The reaper enforces single-mpv (I2) across daemon restarts by the *socket*, not
by a recorded pid. A real listening unix socket stands in for the orphan mpv:
reaping connects, sends the ``quit`` frame, and unlinks the stale path. The
recycling hazard the redesign forecloses -- SIGKILLing a recorded pid that a
reboot may have recycled onto an unrelated process -- is asserted directly: the
reaper never signals a bare pid, and a stale-but-not-listening path is only
unlinked. A probe or unlink error stands nothing; it never escapes ``reap``.

The socket is bound with a short relative name from ``tmp_path`` as the working
directory: an ``AF_UNIX`` path is capped near 104 bytes and the full ``.tmp``
path overflows it, so the tests ``chdir`` and use ``mpv.sock``.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from punt_vox.types_programs.end_file_reason import EndFileReason
from punt_vox.types_programs.mpv_event import MpvCommand
from punt_vox.voxd.programs.mpv.orphan_reaper import OrphanReaper

if TYPE_CHECKING:
    import pytest

_SOCK_NAME = "mpv.sock"


def _listen(path: Path) -> socket.socket:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    return server


def _accept_one(server: socket.socket, received: list[bytes]) -> threading.Thread:
    def _serve() -> None:
        conn, _ = server.accept()
        with conn:
            received.append(conn.recv(4096))

    thread = threading.Thread(target=_serve)
    thread.start()
    return thread


def test_reap_quits_a_responsive_orphan_on_the_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A listening socket is quit cleanly over IPC, then the stale path unlinked.
    monkeypatch.chdir(tmp_path)
    sock = Path(_SOCK_NAME)
    server = _listen(sock)
    received: list[bytes] = []
    thread = _accept_one(server, received)
    try:
        OrphanReaper(sock).reap()
        thread.join(timeout=5)
        assert received, "the orphan received no quit frame"
        payload = json.loads(received[0].decode().strip())
        assert payload["command"] == list(MpvCommand.quit().args)  # a clean quit
        assert not sock.exists()  # the stale socket path was unlinked
    finally:
        server.close()


def test_reap_never_signals_a_bare_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The recycling hazard the redesign kills: reaping must never send a signal
    # to any pid. os.kill is banned for the whole reap -- identity is the socket.
    def _banned(*_args: object, **_kwargs: object) -> None:
        msg = "reap must not signal a pid"
        raise AssertionError(msg)

    monkeypatch.setattr(os, "kill", _banned)
    monkeypatch.chdir(tmp_path)
    sock = Path(_SOCK_NAME)
    server = _listen(sock)
    received: list[bytes] = []
    thread = _accept_one(server, received)
    try:
        OrphanReaper(sock).reap()  # quits by socket, never by pid
        thread.join(timeout=5)
        assert received
    finally:
        server.close()


def test_reap_unlinks_a_stale_socket_with_no_listener(tmp_path: Path) -> None:
    # A path that exists but no longer listens (a stale inode) is just unlinked;
    # there is nothing to quit and nothing to kill.
    sock = tmp_path / _SOCK_NAME
    sock.write_bytes(b"")  # a plain file at the socket path -- no listener
    OrphanReaper(sock).reap()
    assert not sock.exists()


def test_reap_without_a_socket_is_a_noop(tmp_path: Path) -> None:
    OrphanReaper(tmp_path / _SOCK_NAME).reap()  # nothing bound/listening


def test_reap_survives_an_unlink_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An OSError from unlink (a permission error or a race) is logged, never
    # raised into the supervisor -- the reap probe must not become a bring-up
    # crash.
    def _boom(_self: Path, *, missing_ok: bool = False) -> None:
        msg = "permission denied"
        raise PermissionError(msg)

    monkeypatch.setattr(Path, "unlink", _boom)
    sock = tmp_path / _SOCK_NAME
    sock.write_bytes(b"")
    OrphanReaper(sock).reap()  # must not raise


def test_end_file_reason_round_trips() -> None:
    # A guard on the wire enum the reaper's quit relies on: quit is a clean
    # teardown, distinct from the advancing eof outcome.
    assert EndFileReason.from_wire("quit") is EndFileReason.QUIT
    assert EndFileReason.from_wire("eof") is EndFileReason.EOF
