"""``PlaybackSuspension`` -- the daemon's click-free pause/resume seam.

With mpv, pause is not a teardown and resume is not a re-spawn. Pause is a single
``set_property pause`` on the one persistent process: mpv stops feeding the audio
output and resumes from the exact same decoder position, so there is no underrun
click and no wall-clock seek to reconstruct. The suspension therefore sheds the
old resume-point machinery entirely; it holds just the authoritative paused flag
and delegates the click-free IPC to the injected :class:`Player`.

The flag is the one place status reads ``is_paused``. The loop reads it too, at
two points: at the advance decision, as the ``T3`` guard against an in-flight
``eof`` advancing under "paused"; and at load time, to decide whether a reload
loads paused (Fork B / I6) -- for a prev/next while paused and for a post-crash
reload while paused. ``reset`` (a switch or off) clears the flag; the loop's
``stop`` or next ``loadfile`` settles the mpv side, so ``reset`` issues no IPC.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from punt_vox.voxd.programs.player import Player

__all__ = ["PlaybackSuspension"]


@final
class PlaybackSuspension:
    """Hold the paused flag and delegate click-free pause/resume to the player."""

    __slots__ = ("_paused", "_player")
    _paused: bool
    _player: Player

    def __new__(cls, player: Player) -> Self:
        self = super().__new__(cls)
        self._paused = False
        self._player = player
        return self

    @property
    def is_paused(self) -> bool:
        """Return whether the active source is suspended in place (cursor held)."""
        return self._paused

    def pause(self) -> None:
        """Suspend playback where it is -- mpv freezes in place. Idempotent.

        The click-free ``set_property pause`` holds the exact decoder position; no
        teardown, no seek. Dropped by the player when mpv is not ready -- the flag
        still stands, and a post-recovery reload honours it (I6).
        """
        if self._paused:
            return
        self._paused = True
        self._player.pause()

    def resume(self) -> None:
        """Continue from the frozen position -- mpv un-pauses in place. Idempotent."""
        if not self._paused:
            return
        self._paused = False
        self._player.resume()

    def reset(self) -> None:
        """Clear the paused flag on a switch or off; the loop settles the mpv side.

        A stop or a source switch starts the next source fresh, never inheriting
        a displaced album's pause. The loop's ``stop`` (off) or next ``loadfile``
        with ``pause=no`` (switch) drives mpv, so this issues no IPC of its own.
        """
        self._paused = False
