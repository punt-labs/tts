"""The player seam -- load a Part on the mpv connection and control playback.

``ProgramLoop`` owns *when* to play, advance, pause, and stop; ``Player`` owns
*how* those intents become IPC on the one persistent mpv process. Production
injects :class:`~punt_vox.voxd.programs.mpv.mpv_program_player.MpvProgramPlayer`;
tests inject a fake whose ended-futures and control calls they observe, so the
loop's advance/interrupt/pause logic is exercised without a real mpv.

The seam is small and asymmetric on purpose. :meth:`Player.await_ready` and
:meth:`Player.play` are awaited -- they gate on the connection and confirm the
load. :meth:`Player.pause`, :meth:`Player.resume`, and :meth:`Player.stop` are
fire-and-forget control commands: they return at once and are dropped when mpv is
not ready, because the suspension flag and the post-recovery reload carry the
intent (I1, I6). A :class:`PlayHandle` carries one load's ended-future, whose
resolved :class:`~punt_vox.types_programs.mpv_event.EndFileReason` tells the loop
how the part stopped -- a natural ``eof``, a bad-file ``error``, or the synthetic
``crashed`` a socket EOF injects (I7).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from punt_vox.types_programs.mpv_event import EndFileReason
    from punt_vox.voxd.programs.part import Part

__all__ = ["PlayHandle", "Player"]


class PlayHandle(Protocol):
    """One in-flight load whose end the loop races against a control interrupt."""

    async def ended(self) -> EndFileReason:
        """Block until this load ends and return why (``eof``/``error``/``crashed``)."""
        ...


class Player(Protocol):
    """Drive the mpv connection for the loop (single-loadfile-owner)."""

    async def await_ready(self) -> None:
        """Block until mpv is ready to accept commands (the loop's ``WaitReady``)."""
        ...

    async def play(self, part: Part, *, paused: bool) -> PlayHandle:
        """Load ``part`` (paused per the flag) and return its ended-future handle."""
        ...

    def pause(self) -> None:
        """Suspend playback in place, click-free; dropped when mpv is not ready."""
        ...

    def resume(self) -> None:
        """Continue playback from the exact position; dropped when not ready."""
        ...

    def stop(self) -> None:
        """Unload the current file, returning mpv to idle; dropped when not ready."""
        ...
