"""``MpvProgramPlayer`` -- the loop-facing player over the mpv connection.

This is the :class:`~punt_vox.voxd.programs.player.Player` the playback loop
drives. It turns the loop's intents into IPC on whatever connection the
supervisor currently holds:

* :meth:`await_ready` is the loop's ``WaitReady`` -- it parks until mpv is up,
  at startup and after every crash, so a command is never issued into a
  not-ready client (I1).
* :meth:`play` issues the one ``loadfile`` (single-loadfile-ownership) and hands
  back a handle whose ended-future the reader resolves on ``end-file`` -- or, on
  a crash, with the synthetic ``crashed`` reason (I7). Whether the part loads
  paused is set on mpv's global ``pause`` property *before* the load, so a reload
  after a crash-while-paused stays paused (I6) without a per-file option.
* :meth:`pause`/:meth:`resume`/:meth:`stop` are fire-and-forget control commands.
  When mpv is not ready they are dropped -- the suspension flag still flips and
  the post-recovery reload honours it, so a crash mid-pause never wedges a caller.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Self, final

from punt_vox.types_programs.mpv_event import MpvCommand

if TYPE_CHECKING:
    import asyncio

    from punt_vox.types_programs.mpv_event import EndFileReason
    from punt_vox.voxd.programs.mpv.mpv_client import MpvClient
    from punt_vox.voxd.programs.mpv.mpv_supervisor import MpvSupervisor
    from punt_vox.voxd.programs.part import Part
    from punt_vox.voxd.programs.player_directory import PlayerDirectory

__all__ = ["MpvPlayHandle", "MpvProgramPlayer"]


@final
class MpvPlayHandle:
    """A single load: the ended-future the loop races against an interrupt."""

    __slots__ = ("_ended",)
    _ended: asyncio.Future[EndFileReason]

    def __new__(cls, ended: asyncio.Future[EndFileReason]) -> Self:
        self = super().__new__(cls)
        self._ended = ended
        return self

    async def ended(self) -> EndFileReason:
        """Await this load's end -- an ``end-file`` reason, or ``crashed`` (I7)."""
        return await self._ended


@final
class MpvProgramPlayer:
    """Drive the mpv connection for the playback loop (single-loadfile-owner)."""

    __slots__ = ("_directory", "_supervisor")
    _supervisor: MpvSupervisor
    _directory: PlayerDirectory

    def __new__(cls, supervisor: MpvSupervisor, directory: PlayerDirectory) -> Self:
        self = super().__new__(cls)
        self._supervisor = supervisor
        self._directory = directory
        return self

    async def await_ready(self) -> None:
        """Park until mpv is ready -- the loop's ``WaitReady`` step (I1)."""
        await self._supervisor.wait_ready()

    async def play(self, part: Part, *, paused: bool) -> MpvPlayHandle:
        """Load ``part`` (paused per the flag) and return its ended-future handle.

        The ``pause`` property is set before the load so a reload while paused --
        after a crash or a prev/next -- stays paused (I6). ``loadfile`` is
        awaited: the reply confirms mpv queued the file (a wedged mpv surfaces as
        a timeout the loop backs off on), never that it decodes -- a bad file
        surfaces later as an ``end-file`` reason ``error``.
        """
        client = self._require_client()
        path = str(self._directory.locate(part))
        client.write_command(MpvCommand.set_pause(paused=paused))
        ended = client.arm_ended()
        await client.request(MpvCommand.loadfile(path))
        return MpvPlayHandle(ended)

    def pause(self) -> None:
        """Suspend playback in place (click-free); dropped when mpv is not ready."""
        self._send_control(MpvCommand.set_pause(paused=True))

    def resume(self) -> None:
        """Continue playback from the exact position; dropped when not ready."""
        self._send_control(MpvCommand.set_pause(paused=False))

    def stop(self) -> None:
        """Unload the current file, returning mpv to idle; dropped when not ready."""
        self._send_control(MpvCommand.stop())

    def _require_client(self) -> MpvClient:
        """Return the ready connection, or raise so the loop backs off and retries."""
        client = self._supervisor.current_client()
        if client is None:
            msg = "mpv is not ready"
            raise ConnectionError(msg)
        return client

    def _send_control(self, command: MpvCommand) -> None:
        """Fire a control command when ready, swallowing a mid-crash send (I1)."""
        client = self._supervisor.current_client()
        if client is None:
            return
        with contextlib.suppress(ConnectionError, OSError):
            client.write_command(command)
