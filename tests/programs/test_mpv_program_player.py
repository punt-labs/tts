"""Tests for :class:`MpvProgramPlayer` -- the loop-facing player over the client.

The player is the only ``loadfile`` issuer (single-loadfile-ownership). These
tests assert the commands it sends for play/pause/resume/stop against a recording
client: a paused load sets the global ``pause`` property before ``loadfile`` (I6 /
Fork B), and a control command issued while mpv is not ready is dropped (I1).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Self, cast, final

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
class _FakeSupervisor:
    """A supervisor double: hands out a client, or ``None`` when not ready."""

    __slots__ = ("_client",)
    _client: _RecordingClient | None

    def __new__(cls, client: _RecordingClient | None) -> Self:
        self = super().__new__(cls)
        self._client = client
        return self

    def current_client(self) -> _RecordingClient | None:
        return self._client

    async def wait_ready(self) -> None:
        return None


@final
class _FakeDirectory:
    """A PlayerDirectory double resolving every Part to one fixed path."""

    __slots__ = ()

    def locate(self, _part: Part) -> Path:
        return _PATH


def _player(client: _RecordingClient | None) -> MpvProgramPlayer:
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
