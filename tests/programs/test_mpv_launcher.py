"""Tests for :class:`MpvLauncher` -- the mpv spawn/connect bring-up mechanics.

The launcher builds the hardened spawn argv, starts the process, and connects
the IPC socket. Both OS calls are patched, so no real mpv is spawned: the flag
set is asserted directly, and spawn/connect are exercised for their success and
failure branches (a missing binary, a socket that never appears).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd.programs.mpv import mpv_launcher as launcher_mod
from punt_vox.voxd.programs.mpv.mpv_launcher import MpvLauncher

from .conftest import FakeSleeper

if TYPE_CHECKING:
    import pytest

_SOCK = Path("/nonexistent/mpv.sock")


@final
class _FakeProc:
    """A spawned-process double the launcher hands back on a successful spawn."""

    __slots__ = ("pid",)
    pid: int

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self.pid = 424242
        return self


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


def _noop() -> None:
    return None


def test_startup_flags_set_the_reduced_music_volume() -> None:
    # The program tier plays under speech/chimes, so mpv starts at the reduced
    # _MUSIC_VOLUME (60) -- the music half of the two-tier static rebalance.
    flags = MpvLauncher(_SOCK, FakeSleeper())._flags()

    assert f"--volume={launcher_mod._MUSIC_VOLUME}" in flags
    assert "--volume=60" in flags


def test_startup_flags_disable_network_and_script_fetch() -> None:
    # Hardening of the persistent-mpv surface: a crafted media path must not
    # trigger network or script fetching. --ytdl=no kills the youtube-dl URL
    # hook, --load-scripts=no blocks user-script execution, --no-config bars the
    # config dir. loadfile then plays only the contained local file it is given.
    flags = MpvLauncher(_SOCK, FakeSleeper())._flags()

    assert "--ytdl=no" in flags
    assert "--load-scripts=no" in flags
    assert "--no-config" in flags


def test_flags_carry_the_socket_path() -> None:
    # The IPC server flag names the exact socket the supervisor will connect to.
    flags = MpvLauncher(_SOCK, FakeSleeper())._flags()

    assert f"--input-ipc-server={_SOCK}" in flags


async def test_spawn_returns_the_started_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _FakeProc()

    async def _spawn(*_args: object, **_kwargs: object) -> _FakeProc:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)

    result = await MpvLauncher(_SOCK, FakeSleeper()).spawn()

    assert result is not None
    assert result.pid == proc.pid  # the launcher handed back the spawned process


async def test_spawn_missing_binary_returns_none_and_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A missing/too-old mpv raises OSError from exec; spawn must fold that into a
    # None (bring-up failure) and leave a greppable error, never propagate.
    async def _spawn_boom(*_args: object, **_kwargs: object) -> object:
        msg = "no such binary"
        raise OSError(msg)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn_boom)

    with caplog.at_level(logging.ERROR):
        result = await MpvLauncher(_SOCK, FakeSleeper()).spawn()

    assert result is None
    assert any("mpv spawn failed" in r.getMessage() for r in caplog.records)


async def test_connect_returns_a_started_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _connect_factory() -> tuple[asyncio.StreamReader, _FakeWriter]:
        return asyncio.StreamReader(), _FakeWriter()

    async def _connect(
        *_args: object, **_kwargs: object
    ) -> tuple[asyncio.StreamReader, _FakeWriter]:
        return _connect_factory()

    monkeypatch.setattr(asyncio, "open_unix_connection", _connect)

    client = await MpvLauncher(_SOCK, FakeSleeper()).connect(_noop)

    assert client is not None
    assert client.is_ready
    await client.close()  # stop the reader task the connection started


async def test_connect_exhaustion_returns_none_and_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A socket that never opens exhausts the retry loop; connect returns None
    # and leaves a durable trace, symmetric with the spawn-failure error log.
    async def _connect_fail(*_args: object, **_kwargs: object) -> object:
        msg = "no socket"
        raise OSError(msg)

    monkeypatch.setattr(asyncio, "open_unix_connection", _connect_fail)

    with caplog.at_level(logging.ERROR):
        client = await MpvLauncher(_SOCK, FakeSleeper()).connect(_noop)

    assert client is None
    assert any("connect failed after" in r.getMessage() for r in caplog.records)
