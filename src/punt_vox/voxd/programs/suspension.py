"""``PlaybackSuspension`` -- the daemon's pause/resume seam over the live player.

Pause suspends the *running* player process in place (``SIGSTOP``) and holds a
gate the playback loop waits on before it spawns the next Part; resume continues
the process (``SIGCONT``) and opens the gate. Because a ``SIGSTOP``-ed player never
exits, the loop's ``proc.wait`` stays pending while paused -- so the cursor never
auto-advances (Z ``T3``: a paused album is suspended). The transport's prev/next
interrupt that suspended wait to reposition the held Part; the gate keeps the loop
from playing the newly-cursored Part until resume, so repositioning a paused player
moves the cursor *without un-suspending it* (Z Fork B).

The suspension is shared by the loop (which ``attach``es each freshly spawned
handle and ``detach``es it when the track settles) and the ``ProgramService`` (which
drives ``pause``/``resume`` and reads :attr:`is_paused` for the status projection).
It is the one place the paused flag lives, so ``status`` reads it authoritatively.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from punt_vox.voxd.programs.player import PlayerProcess

__all__ = ["PlaybackSuspension"]


@final
class PlaybackSuspension:
    """Hold the paused flag, the loop gate, and the live handle to suspend."""

    __slots__ = ("_gate", "_handle", "_paused")
    _paused: bool
    _gate: asyncio.Event
    # The player process the loop is currently racing; None between tracks.
    _handle: PlayerProcess | None

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._paused = False
        self._gate = asyncio.Event()
        self._gate.set()  # not paused -> the loop may spawn the next Part
        self._handle = None
        return self

    @property
    def is_paused(self) -> bool:
        """Return whether the active source is suspended in place."""
        return self._paused

    def pause(self) -> None:
        """Suspend the held player in place; hold the loop gate. Idempotent."""
        if self._paused:
            return
        self._paused = True
        self._gate.clear()
        if self._handle is not None:
            self._handle.suspend()

    def resume(self) -> None:
        """Continue the suspended player; open the loop gate. Idempotent."""
        if not self._paused:
            return
        self._paused = False
        self._gate.set()
        if self._handle is not None:
            self._handle.resume()

    def reset(self) -> None:
        """Return to the not-paused, unheld state (a stop or a source switch).

        The held handle is continued first so a ``SIGSTOP``-ed player is never left
        stopped when the loop tears it down -- a stopped orphan the OS would keep
        around. This runs at the daemon's source-lifecycle boundary (off, switch).
        """
        if self._handle is not None and self._paused:
            self._handle.resume()
        self._paused = False
        self._gate.set()
        self._handle = None

    def attach(self, handle: PlayerProcess) -> None:
        """Register a freshly spawned handle; suspend it now if already paused.

        The suspend-if-paused covers the narrow window where ``pause`` landed while
        the loop was awaiting the spawn: the gate held the *next* spawn, but this
        one was already in flight, so it must be suspended on arrival.
        """
        self._handle = handle
        if self._paused:
            handle.suspend()

    def detach(self) -> None:
        """Forget the handle once its track has settled (killed or ended)."""
        self._handle = None

    async def wait_resumed(self) -> None:
        """Block the playback loop while paused; return at once when playing."""
        await self._gate.wait()
