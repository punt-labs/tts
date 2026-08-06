"""Tests for :class:`MpvSupervisor` -- the mpv process/connection lifecycle.

The real ``run`` loop is driven with spawn and connect patched, so no real mpv is
spawned. The modeled invariants are asserted by name: I4 (the restart cap
terminates at ``failed``), I3 (a standing fault holds only in the fault modes),
ready-on-connect, and crash recovery through a fresh connection (I5). The pinned
minimum mpv version -- the contract ``doctor`` imports -- is checked too.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from punt_vox.types_programs.playback_fault import PlaybackFaultKind
from punt_vox.voxd.programs.mpv import MPV_MIN_VERSION, mpv_supervisor as sup_mod
from punt_vox.voxd.programs.mpv.mpv_supervisor import MpvState, MpvSupervisor
from punt_vox.voxd.programs.mpv.orphan_reaper import (
    OrphanReaper,
    OrphanUnreachableError,
)

from .conftest import FakeSleeper

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

_SOCK = Path("/nonexistent/mpv.sock")


@final
class _FakeProc:
    """A spawned-process double: alive, killable, with a stable pid."""

    __slots__ = ("pid", "returncode")
    pid: int
    returncode: int | None

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self.pid = 424242
        self.returncode = None
        return self

    def kill(self) -> None:
        self.returncode = -9


@final
class _FakeWriter:
    """A StreamWriter double the connection writes control frames to."""

    __slots__ = ("closed",)
    closed: bool

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self.closed = False
        return self

    def write(self, _data: bytes) -> None:
        return None

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


async def _wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(5000):
        if predicate():
            return
        await asyncio.sleep(0)
    msg = "condition never held"
    raise AssertionError(msg)


async def _cancel(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


def _patch_spawn(monkeypatch: pytest.MonkeyPatch, spawns: list[int]) -> None:
    async def _spawn(*_args: object, **_kwargs: object) -> _FakeProc:
        spawns.append(1)
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)


def test_min_version_is_a_pinned_three_tuple() -> None:
    # doctor imports this to gate an installed mpv; it must be a version triple.
    assert len(MPV_MIN_VERSION) == 3
    assert all(isinstance(part, int) for part in MPV_MIN_VERSION)


async def test_cold_start_that_never_connects_reaches_failed_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # I4: consecutive attempts that never reach ready climb the cap and terminate
    # at ``failed``; I3: the standing fault is PLAYER_UNAVAILABLE (never was ready);
    # I2: one spawn per attempt (cap+1), never two live processes at once.
    spawns: list[int] = []
    _patch_spawn(monkeypatch, spawns)

    async def _connect_fail(*_args: object, **_kwargs: object) -> object:
        msg = "no socket"
        raise OSError(msg)

    monkeypatch.setattr(asyncio, "open_unix_connection", _connect_fail)

    supervisor = MpvSupervisor(_SOCK, FakeSleeper())
    run = asyncio.create_task(supervisor.run())
    await _wait_until(lambda: supervisor.state is MpvState.FAILED)

    assert supervisor.is_ready is False
    fault = supervisor.fault
    assert fault is not None
    assert fault.kind is PlaybackFaultKind.PLAYER_UNAVAILABLE
    assert fault.part_index == 0  # a process-level fault names no part
    assert len(spawns) == sup_mod._MAX_RESTARTS + 1
    await _cancel(run)


async def test_connect_exhaustion_logs_an_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A connect that never succeeds must leave a durable trace, symmetric with the
    # spawn-failure error log, so both bring-up failure modes are greppable.
    _patch_spawn(monkeypatch, [])

    async def _connect_fail(*_args: object, **_kwargs: object) -> object:
        msg = "no socket"
        raise OSError(msg)

    monkeypatch.setattr(asyncio, "open_unix_connection", _connect_fail)

    supervisor = MpvSupervisor(_SOCK, FakeSleeper())
    with caplog.at_level(logging.ERROR):
        run = asyncio.create_task(supervisor.run())
        await _wait_until(lambda: supervisor.state is MpvState.FAILED)
    assert any("connect failed after" in r.getMessage() for r in caplog.records)
    await _cancel(run)


async def test_unexpected_error_stands_a_fault_and_does_not_hang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An unexpected error in the supervise loop (a supervisor bug, not a modeled
    # crash) must be caught: stand a hard PLAYER_FAILED fault so status reports it,
    # never leave the task dead with wait_ready hanging and no fault set.
    async def _spawn_boom(*_args: object, **_kwargs: object) -> object:
        msg = "supervisor bug"
        raise RuntimeError(msg)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn_boom)

    supervisor = MpvSupervisor(_SOCK, FakeSleeper())
    run = asyncio.create_task(supervisor.run())
    await _wait_until(lambda: supervisor.state is MpvState.FAILED)

    fault = supervisor.fault
    assert fault is not None  # observable via status, not a silent hang
    assert fault.kind is PlaybackFaultKind.PLAYER_FAILED
    assert supervisor.is_ready is False
    assert not run.done()  # parked on the standing fault, not crashed out
    await _cancel(run)


def _patch_connect_ok(
    monkeypatch: pytest.MonkeyPatch,
    readers: list[asyncio.StreamReader],
    writers: list[_FakeWriter],
) -> None:
    async def _connect(
        *_args: object, **_kwargs: object
    ) -> tuple[asyncio.StreamReader, _FakeWriter]:
        reader = asyncio.StreamReader()
        writer = _FakeWriter()
        readers.append(reader)
        writers.append(writer)
        return reader, writer

    monkeypatch.setattr(asyncio, "open_unix_connection", _connect)


async def test_successful_bring_up_reaches_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_spawn(monkeypatch, [])
    _patch_connect_ok(monkeypatch, [], [])

    supervisor = MpvSupervisor(_SOCK, FakeSleeper())
    run = asyncio.create_task(supervisor.run())
    await _wait_until(lambda: supervisor.is_ready)

    assert supervisor.state is MpvState.READY
    assert supervisor.fault is None  # I3: ready carries no standing fault
    assert supervisor.current_client() is not None
    await _cancel(run)


async def test_crash_then_reconnect_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A crash (socket EOF) clears ready and routes through a restart; a fresh
    # connection reaches ready again (I5: one reader per connection).
    _patch_spawn(monkeypatch, [])
    readers: list[asyncio.StreamReader] = []
    _patch_connect_ok(monkeypatch, readers, [])

    supervisor = MpvSupervisor(_SOCK, FakeSleeper())
    run = asyncio.create_task(supervisor.run())
    await _wait_until(lambda: supervisor.is_ready)

    readers[0].feed_eof()  # the process died -- the reader sees socket EOF
    await _wait_until(lambda: len(readers) >= 2 and supervisor.is_ready)

    assert supervisor.is_ready  # reconnected after the crash
    assert supervisor.fault is None  # a recovered crash clears the fault
    await _cancel(run)


async def test_crash_closes_the_dead_clients_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # D: a crash closes the dead client's connection rather than leaking the
    # StreamWriter to GC; the reconnect then opens a fresh one.
    _patch_spawn(monkeypatch, [])
    readers: list[asyncio.StreamReader] = []
    writers: list[_FakeWriter] = []
    _patch_connect_ok(monkeypatch, readers, writers)

    supervisor = MpvSupervisor(_SOCK, FakeSleeper())
    run = asyncio.create_task(supervisor.run())
    await _wait_until(lambda: supervisor.is_ready)

    readers[0].feed_eof()  # the process died -- socket EOF
    await _wait_until(lambda: writers[0].closed)  # the dead client's socket closed

    assert writers[0].closed
    await _cancel(run)


async def test_shutdown_teardown_clears_the_standing_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # C / I3 (strict): a fault holds only in the fault modes. Shutdown drives the
    # process to ``down`` and must clear the standing fault, never surface
    # ``down`` alongside a fault.
    _patch_spawn(monkeypatch, [])

    async def _connect_fail(*_args: object, **_kwargs: object) -> object:
        msg = "no socket"
        raise OSError(msg)

    monkeypatch.setattr(asyncio, "open_unix_connection", _connect_fail)

    supervisor = MpvSupervisor(_SOCK, FakeSleeper())
    run = asyncio.create_task(supervisor.run())
    await _wait_until(lambda: supervisor.state is MpvState.FAILED)
    assert supervisor.fault is not None  # a standing fault in the failed mode

    await _cancel(run)  # shutdown cancels run -> teardown to ``down``
    assert supervisor.state is MpvState.DOWN
    assert supervisor.fault is None  # I3: ``down`` carries no fault


async def test_eacces_orphan_probe_aborts_bring_up_without_spawning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An orphan socket whose probe is denied (EACCES) must NOT let a second mpv
    # spawn (I2). reap raises OrphanUnreachableError, so _bring_up aborts BEFORE
    # the spawn and folds into the restart-to-failed path: zero spawns, and the
    # standing fault is PLAYER_UNAVAILABLE (mpv never came up).
    def _denied(_self: OrphanReaper) -> None:
        msg = "denied"
        raise OrphanUnreachableError(msg)

    monkeypatch.setattr(OrphanReaper, "reap", _denied)
    spawns: list[int] = []
    _patch_spawn(monkeypatch, spawns)

    supervisor = MpvSupervisor(_SOCK, FakeSleeper())
    run = asyncio.create_task(supervisor.run())
    await _wait_until(lambda: supervisor.state is MpvState.FAILED)

    assert spawns == []  # never spawned an mpv while an orphan may hold the socket
    fault = supervisor.fault
    assert fault is not None
    assert fault.kind is PlaybackFaultKind.PLAYER_UNAVAILABLE
    await _cancel(run)
