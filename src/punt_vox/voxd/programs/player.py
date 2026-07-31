"""The player seam -- spawn a process for a Part and control its lifetime.

``ProgramLoop`` owns *when* to play, advance, and stop; ``Player`` owns *how* a
Part becomes a running process. Production injects a subprocess player; tests
inject a fake whose process end they control, so the loop's advance/interrupt
logic is exercised without a real subprocess.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from punt_vox.voxd.programs.part import Part

__all__ = ["Player", "PlayerProcess"]


class PlayerProcess(Protocol):
    """A running player whose end the loop waits for, stops, or cuts short."""

    async def wait(self) -> int:
        """Block until the player exits and return its exit code."""
        ...

    async def kill(self) -> None:
        """Stop the player now (a skip / off / play-a-part interrupt)."""
        ...

    def stop_gracefully(self) -> None:
        """Ask the player to exit cleanly (``SIGTERM``) for a click-free pause.

        The player closes its audio device on the way out, so the device stops
        with no underrun -- unlike a ``SIGSTOP`` freeze. The loop reaps the exit
        through its usual wait; the pause path never advances the cursor.
        """
        ...

    def terminate(self) -> None:
        """Kill the player now, synchronously (daemon shutdown teardown).

        A sync ``SIGKILL`` so ``shutdown`` -- which runs outside the event loop --
        can tear down a player caught mid-spawn without awaiting; no orphan
        lingers after the daemon exits.
        """
        ...


class Player(Protocol):
    """Turn a ready Part into a running :class:`PlayerProcess` (PY-DP-11)."""

    async def play(self, part: Part, offset: float = 0.0) -> PlayerProcess:
        """Start playing ``part`` seeked to ``offset`` seconds; return the handle."""
        ...
