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
        awaited and its reply is checked: a command-level rejection (``error !=
        "success"``) emits no ``end-file``, so discarding the reply would wedge
        the loop on an ended-future that never resolves while ``status`` reports
        healthy. A rejection is raised as :class:`ConnectionError`, routing into
        the loop's ``_back_off_load`` (``PLAYER_UNAVAILABLE``) symmetric with a
        wedged (timeout) or crashed (lost) connection. A reply of ``success``
        confirms only that mpv queued the file, never that it decodes -- a bad
        file surfaces later as an ``end-file`` reason ``error``.

        The ended-future is armed *after* the load is acknowledged, never before:
        a prev/next issued near the previous part's natural end can leave an
        ``end-file`` eof in flight, and mpv emits it before the ``loadfile``
        reply on the one ordered socket. Arming after the reply means that stale
        eof resolves the prior load's future (harmlessly -- the loop has already
        abandoned it), not the freshly-loaded track's; arming before it would let
        the stale eof resolve the new track's future, and the loop would advance
        past a part it never heard play. mpv cannot end the new part before it
        starts, so no genuine end is missed in the window.
        """
        client = self._require_client()
        # ``locate`` gates the Part path through the shared containment check: an
        # untrusted manifest file field that is a symlink or escapes the album
        # directory raises ``ValueError`` here, before ``loadfile`` -- the loop
        # treats that refusal like a bad file (skip), so no crafted path loads.
        path = str(self._directory.locate(part))
        client.write_command(MpvCommand.set_pause(paused=paused))
        response = await client.request(MpvCommand.loadfile(path))
        if not response.ok:
            msg = f"mpv rejected loadfile: {response.error}"
            raise ConnectionError(msg)
        return MpvPlayHandle(client.arm_ended())

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
