"""Tests for :class:`OrphanReaper` -- reaping an mpv left by an unclean exit.

The reaper enforces single-mpv (I2) across daemon restarts. These tests drive a
real short-lived child process as the stand-in orphan: recording its pid then
reaping kills it and clears the stale socket/pidfile, while a dead or absent pid
is a no-op that still tidies the paths.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import TYPE_CHECKING

from punt_vox.voxd.programs.mpv.orphan_reaper import OrphanReaper

if TYPE_CHECKING:
    from pathlib import Path


def _spawn_sleeper() -> subprocess.Popen[bytes]:
    return subprocess.Popen(["sleep", "30"])


def _is_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_dead(proc: subprocess.Popen[bytes]) -> None:
    for _ in range(200):
        if proc.poll() is not None:
            return
        time.sleep(0.01)
    proc.kill()
    msg = "orphan was not reaped"
    raise AssertionError(msg)


def test_reap_kills_a_recorded_live_orphan(tmp_path: Path) -> None:
    sock = tmp_path / "mpv.sock"
    sock.write_bytes(b"")  # a stale socket path the reaper should clear
    reaper = OrphanReaper(sock)
    orphan = _spawn_sleeper()
    try:
        reaper.record(orphan.pid)
        reaper.reap()
        _wait_dead(orphan)  # the orphan received SIGKILL
        assert not sock.exists()  # the stale socket path was cleared
        assert not (tmp_path / "mpv.sock.pid").exists()  # pidfile cleared
    finally:
        if orphan.poll() is None:
            orphan.kill()
            orphan.wait()


def test_reap_is_a_noop_when_the_recorded_pid_is_dead(tmp_path: Path) -> None:
    sock = tmp_path / "mpv.sock"
    reaper = OrphanReaper(sock)
    dead = _spawn_sleeper()
    dead.kill()
    dead.wait()
    assert not _is_live(dead.pid)
    reaper.record(dead.pid)
    reaper.reap()  # a dead pid must not raise and must clear the pidfile
    assert not (tmp_path / "mpv.sock.pid").exists()


def test_reap_without_a_record_is_a_noop(tmp_path: Path) -> None:
    OrphanReaper(tmp_path / "mpv.sock").reap()  # nothing recorded/listening


def test_clear_removes_the_pidfile(tmp_path: Path) -> None:
    reaper = OrphanReaper(tmp_path / "mpv.sock")
    reaper.record(4242)
    assert (tmp_path / "mpv.sock.pid").exists()
    reaper.clear()
    assert not (tmp_path / "mpv.sock.pid").exists()
