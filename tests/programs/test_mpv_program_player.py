"""Tests for :class:`MpvProgramPlayer` -- the loop-facing player over the client.

The player is the only ``loadfile`` issuer (single-loadfile-ownership). These
tests assert the commands it sends for play/pause/resume/stop against a recording
client: a paused load sets the global ``pause`` property before ``loadfile`` (I6 /
Fork B), and a control command issued while mpv is not ready is dropped (I1).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Self, cast, final

import pytest

from punt_vox.types_programs.mpv_event import EndFileReason, MpvArg, MpvResponse
from punt_vox.voxd.programs.mpv.mpv_program_player import MpvProgramPlayer

if TYPE_CHECKING:
    from punt_vox.types_programs.mpv_event import MpvCommand
    from punt_vox.voxd.programs.mpv.mpv_supervisor import MpvSupervisor
    from punt_vox.voxd.programs.part import Part
    from punt_vox.voxd.programs.player_directory import PlayerDirectory

from punt_vox.voxd.programs.part import Part as _Part

_PATH = Path("/m/1.mp3")


class _LiveClient(Protocol):
    """The client surface the player drives -- the seam the doubles implement."""

    @property
    def is_ready(self) -> bool: ...

    def write_command(self, command: MpvCommand) -> None: ...

    async def request(self, command: MpvCommand) -> MpvResponse: ...

    def arm_ended(self) -> asyncio.Future[EndFileReason]: ...


@final
class _RecordingClient:
    """A live-client double recording every command the player issues."""

    __slots__ = ("_reply_error", "commands")
    commands: list[tuple[MpvArg, ...]]
    _reply_error: str

    def __new__(cls, reply_error: str = "success") -> Self:
        self = super().__new__(cls)
        self.commands = []
        self._reply_error = reply_error
        return self

    @property
    def is_ready(self) -> bool:
        return True

    def write_command(self, command: MpvCommand) -> None:
        self.commands.append(command.args)

    async def request(self, command: MpvCommand) -> MpvResponse:
        self.commands.append(command.args)
        return MpvResponse(request_id=1, error=self._reply_error)

    def arm_ended(self) -> asyncio.Future[EndFileReason]:
        return asyncio.get_running_loop().create_future()


@final
class _StaleEofClient:
    """A live-client double where a stale ``end-file`` lands during the loadfile.

    Mirrors :class:`MpvClient`: ``arm_ended`` installs the current ended-future,
    and the reader resolves whichever future is armed. Here ``request`` resolves
    the armed future mid-loadfile, standing in for an in-flight ``end-file`` eof
    from the previous, still-loaded part. A correct player arms the new track's
    future only after the load is acknowledged, so the stale eof resolves the
    prior load's future, never the freshly-loaded track's.
    """

    __slots__ = ("_current",)
    # None until the first load arms a future -- genuinely absent, not a failure.
    _current: asyncio.Future[EndFileReason] | None

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._current = None
        return self

    @property
    def is_ready(self) -> bool:
        return True

    @property
    def current(self) -> asyncio.Future[EndFileReason] | None:
        """Return the currently-armed ended-future (the newest load's)."""
        return self._current

    def write_command(self, command: MpvCommand) -> None:
        return None

    def arm_ended(self) -> asyncio.Future[EndFileReason]:
        fut: asyncio.Future[EndFileReason] = asyncio.get_running_loop().create_future()
        self._current = fut
        return fut

    async def request(self, command: MpvCommand) -> MpvResponse:
        if self._current is not None and not self._current.done():
            self._current.set_result(EndFileReason.EOF)
        return MpvResponse(request_id=1, error="success")


@final
class _FakeSupervisor:
    """A supervisor double: hands out a client, or ``None`` when not ready."""

    __slots__ = ("_client",)
    _client: _LiveClient | None

    def __new__(cls, client: _LiveClient | None) -> Self:
        self = super().__new__(cls)
        self._client = client
        return self

    def current_client(self) -> _LiveClient | None:
        return self._client

    async def wait_ready(self) -> None:
        return None


@final
class _FakeDirectory:
    """A PlayerDirectory double resolving every Part to one fixed path."""

    __slots__ = ()

    def locate(self, _part: Part) -> Path:
        return _PATH


def _player(client: _LiveClient | None) -> MpvProgramPlayer:
    supervisor = cast("MpvSupervisor", _FakeSupervisor(client))
    directory = cast("PlayerDirectory", _FakeDirectory())
    return MpvProgramPlayer(supervisor, directory)


def _part() -> Part:
    return _Part("id1", 1)


async def test_play_sets_pause_false_then_loadfiles() -> None:
    client = _RecordingClient()
    await _player(client).play(_part(), paused=False)
    assert client.commands == [
        ("set_property", "pause", False),
        ("loadfile", str(_PATH), "replace"),
    ]


async def test_paused_play_sets_pause_true_before_loadfile() -> None:
    # I6 / Fork B: a reload while paused sets the global pause property before the
    # load, so recovery (crash-while-paused, prev/next-while-paused) stays paused.
    client = _RecordingClient()
    await _player(client).play(_part(), paused=True)
    assert client.commands[0] == ("set_property", "pause", True)
    assert client.commands[1] == ("loadfile", str(_PATH), "replace")


async def test_pause_resume_stop_issue_their_commands() -> None:
    client = _RecordingClient()
    player = _player(client)
    player.pause()
    player.resume()
    player.stop()
    assert client.commands == [
        ("set_property", "pause", True),
        ("set_property", "pause", False),
        ("stop",),
    ]


async def test_controls_are_dropped_when_not_ready() -> None:
    # I1: no command is issued into a not-ready connection; the flag/recovery carry
    # the intent instead, so a crash mid-pause never wedges the caller.
    player = _player(None)
    player.pause()
    player.resume()
    player.stop()  # nothing to assert but a client -- there is none, so no raise


async def test_play_when_not_ready_raises_so_the_loop_backs_off() -> None:
    with pytest.raises(ConnectionError):
        await _player(None).play(_part(), paused=False)


async def test_rejected_loadfile_raises_so_the_loop_backs_off() -> None:
    # A command-level rejection (error != "success") emits no end-file; discarding
    # the reply would wedge the loop forever while status reports healthy. The
    # player raises ConnectionError so the loop routes into _back_off_load.
    client = _RecordingClient(reply_error="unknown command")
    with pytest.raises(ConnectionError, match="mpv rejected loadfile: unknown command"):
        await _player(client).play(_part(), paused=False)


async def test_stale_eof_during_loadfile_does_not_finish_the_new_track() -> None:
    # An end-file eof from the still-playing previous part can land while the new
    # track's loadfile is in flight. The player must arm the new track's future
    # only after the load is acknowledged, so the stale eof resolves the PRIOR
    # load's future, never the freshly-loaded track's -- otherwise the loop sees
    # the new part as already ended and skips past it after a next/prev near end.
    client = _StaleEofClient()
    previous = client.arm_ended()  # the previous part's still-armed future
    handle = await _player(client).play(_part(), paused=False)

    assert previous.done()  # the stale eof resolved the prior load, not the new one
    new_future = client.current
    assert new_future is not None
    assert new_future is not previous  # a fresh future was armed for the new track
    assert not new_future.done()  # the new track's future is untouched by the eof

    # The handle awaits exactly that fresh future -- the new track ends on its own.
    new_future.set_result(EndFileReason.ERROR)
    assert await asyncio.wait_for(handle.ended(), 1.0) is EndFileReason.ERROR
