"""Tests for :class:`MpvClient` -- one live mpv IPC connection.

The client is exercised over a real connected socket pair: the test plays the
"mpv side", writing framed responses and events and reading the client's
commands. This drives the real reader coroutine, so correlation, the ended-future
resolution, the teardown-reason drop, and the crash path (I7) are covered against
real stream I/O rather than a mock.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from typing import TYPE_CHECKING, Self, final

import pytest

from punt_vox.types_programs.mpv_event import EndFileReason, MpvCommand
from punt_vox.voxd.programs.mpv.mpv_client import MpvClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@final
class _Crashes:
    """A crash-callback recorder."""

    __slots__ = ("count",)
    count: int

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self.count = 0
        return self

    def __call__(self) -> None:
        self.count += 1


@final
class _Link:
    """A connected client + the mpv-side streams the test drives."""

    __slots__ = ("client", "crashes", "mpv_reader", "mpv_writer")
    client: MpvClient
    crashes: _Crashes
    mpv_reader: asyncio.StreamReader
    mpv_writer: asyncio.StreamWriter

    def __new__(
        cls,
        client: MpvClient,
        crashes: _Crashes,
        mpv_reader: asyncio.StreamReader,
        mpv_writer: asyncio.StreamWriter,
    ) -> Self:
        self = super().__new__(cls)
        self.client = client
        self.crashes = crashes
        self.mpv_reader = mpv_reader
        self.mpv_writer = mpv_writer
        return self

    def send(self, obj: dict[str, object]) -> None:
        """Write a framed JSON message from the mpv side to the client."""
        self.mpv_writer.write((json.dumps(obj) + "\n").encode())

    async def read_command(self) -> dict[str, object]:
        """Read one framed command the client wrote."""
        line = await asyncio.wait_for(self.mpv_reader.readline(), 1.0)
        parsed: dict[str, object] = json.loads(line)
        return parsed


@pytest.fixture
async def link() -> AsyncIterator[_Link]:
    """Build a live client wired to an mpv-side socket, torn down after the test."""
    client_sock, mpv_sock = socket.socketpair()
    client_reader, client_writer = await asyncio.open_connection(sock=client_sock)
    mpv_reader, mpv_writer = await asyncio.open_connection(sock=mpv_sock)
    crashes = _Crashes()
    client = MpvClient(client_reader, client_writer, crashes)
    client.start()
    yield _Link(client, crashes, mpv_reader, mpv_writer)
    with contextlib.suppress(Exception):
        await client.close()
    with contextlib.suppress(Exception):
        mpv_writer.close()


async def test_request_correlates_a_response(link: _Link) -> None:
    task = asyncio.create_task(link.client.request(MpvCommand.loadfile("/m/1.mp3")))
    sent = await link.read_command()
    assert sent["command"] == ["loadfile", "/m/1.mp3", "replace"]
    link.send({"request_id": sent["request_id"], "error": "success"})
    await link.mpv_writer.drain()
    response = await asyncio.wait_for(task, 1.0)
    assert response.ok is True


async def test_write_command_sends_without_awaiting_a_reply(link: _Link) -> None:
    link.client.write_command(MpvCommand.set_pause(paused=True))
    sent = await link.read_command()
    assert sent["command"] == ["set_property", "pause", True]


async def test_eof_reason_resolves_the_ended_future(link: _Link) -> None:
    ended = link.client.arm_ended()
    link.send({"event": "end-file", "reason": "eof"})
    await link.mpv_writer.drain()
    assert await asyncio.wait_for(ended, 1.0) is EndFileReason.EOF


async def test_teardown_reason_is_dropped(link: _Link) -> None:
    # A ``stop`` is our own teardown; resolving it would spuriously reload, so the
    # reader drops it and the ended-future stays armed for the real natural end.
    ended = link.client.arm_ended()
    link.send({"event": "end-file", "reason": "stop"})
    await link.mpv_writer.drain()
    for _ in range(10):
        await asyncio.sleep(0)
    assert not ended.done()


async def test_a_malformed_line_does_not_kill_the_reader(link: _Link) -> None:
    link.mpv_writer.write(b"not json\n")
    await link.mpv_writer.drain()
    ended = link.client.arm_ended()
    link.send({"event": "end-file", "reason": "eof"})
    await link.mpv_writer.drain()
    assert await asyncio.wait_for(ended, 1.0) is EndFileReason.EOF


async def test_a_non_conformant_response_does_not_kill_the_reader(link: _Link) -> None:
    # A: a response-shaped line missing ``error`` raises out of the wire accessors;
    # the reader logs and drops it rather than exiting and firing a spurious crash
    # (a needless supervisor restart). A real end still resolves afterwards.
    link.send({"request_id": 5})  # no ``error`` -> MpvResponse.from_object raises
    await link.mpv_writer.drain()
    ended = link.client.arm_ended()
    link.send({"event": "end-file", "reason": "eof"})
    await link.mpv_writer.drain()
    assert await asyncio.wait_for(ended, 1.0) is EndFileReason.EOF
    assert link.crashes.count == 0  # no _on_eof fired -- reader stayed alive


async def test_an_unknown_end_file_reason_resolves_and_advances(link: _Link) -> None:
    # B: a newer mpv can emit an ``end-file`` reason this enum does not name
    # (``unknown``). It must resolve the ended-future -- folded to the advancing
    # ``eof`` class -- never leave the loop hung on the current part.
    ended = link.client.arm_ended()
    link.send({"event": "end-file", "reason": "unknown"})
    await link.mpv_writer.drain()
    assert await asyncio.wait_for(ended, 1.0) is EndFileReason.EOF


async def test_socket_eof_fails_pending_and_crashes_the_ended_future(
    link: _Link,
) -> None:
    # I7: a crash resolves every pending command future AND the loop's ended-future
    # (with ``crashed``), and signals the supervisor -- no await is left orphaned.
    ended = link.client.arm_ended()
    pending = asyncio.create_task(link.client.request(MpvCommand.stop()))
    await link.read_command()  # consume the command; never reply
    link.mpv_writer.close()  # EOF to the client's reader
    with contextlib.suppress(Exception):
        await link.mpv_writer.wait_closed()

    assert await asyncio.wait_for(ended, 1.0) is EndFileReason.CRASHED
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(pending, 1.0)
    assert link.crashes.count == 1
    assert link.client.is_ready is False
