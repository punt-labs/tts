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
    """A running player whose end the loop waits for, suspends, or cuts short."""

    async def wait(self) -> int:
        """Block until the player exits and return its exit code."""
        ...

    async def kill(self) -> None:
        """Stop the player now (a skip / off / play-a-part interrupt)."""
        ...

    def suspend(self) -> None:
        """Pause the player in place (``SIGSTOP``); it stops progressing."""
        ...

    def resume(self) -> None:
        """Continue a suspended player (``SIGCONT``) from where it stopped."""
        ...

    def terminate(self) -> None:
        """Kill the player now, synchronously (daemon shutdown teardown).

        A sync ``SIGKILL`` so ``shutdown`` -- which runs outside the event loop --
        can tear down a still-suspended player without awaiting; ``SIGKILL``
        terminates even a ``SIGSTOP``-ed process, so no orphan lingers to be
        ``SIGCONT``-ed after the daemon exits.
        """
        ...


class Player(Protocol):
    """Turn a ready Part into a running :class:`PlayerProcess` (PY-DP-11)."""

    async def play(self, part: Part) -> PlayerProcess:
        """Start playing ``part`` and return its process handle."""
        ...
