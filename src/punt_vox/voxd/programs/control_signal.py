"""The ``ControlSignal`` command interface -- one serialized source mutation.

Every mutation of the active playback source -- a user command (turn on, rotate,
retune, off), an automatic advance, a switch to a replay Selection, or a fill
outcome from the :class:`Filler` -- is a ``ControlSignal`` posted to the
single-consumer :class:`ControlChannel`. The consumer applies signals one at a
time, so the Z sequential semantics are real: no two commands ever interleave
against a stale source. Each signal is a typed command that knows its own
transition (Command pattern, PY-DP-11). A generate-family signal narrows
``isinstance(source, Program)`` and rejects as a lost race when the active source
is a consume-only :class:`SelectionPlayback`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from punt_vox.voxd.programs.playback_source import PlaybackSource

__all__ = ["ControlSignal"]


class ControlSignal(Protocol):
    """A single command applied to the active source by the sole control consumer."""

    @property
    def interrupts(self) -> bool:
        """Whether applying this command should stop current playback at once.

        ``True`` for skip / next / play-a-part / off (act now); ``False`` for a
        retune (finish the current track first) and for fill outcomes (they
        never cut off what is playing).
        """
        ...

    def apply(self, source: PlaybackSource, /) -> bool:
        """Apply this command's transition to ``source`` (positional-only write).

        Return whether the command took effect on playback. ``True`` means the
        current track should be interrupted when :attr:`interrupts` is set;
        ``False`` marks a modelled no-op -- a transport step that stalled at a
        pool boundary (Z ``Next`` at M / ``Prev`` at 1) or a dropped stale
        outcome -- which must leave the current track playing untouched even
        though the command type nominally interrupts.
        """
        ...
