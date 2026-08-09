"""``MusicStateView`` -- the music fields every MCP status surface reports.

Two ``mic`` tools answer "what is the music doing?": ``status`` (the whole
session, of which music is one block) and ``music`` with ``subcommand="status"``
(music alone). Both report the daemon's authoritative ``program`` block and the
coarse ``music_mode`` label derived from it, so the projection lives here once
rather than in each tool -- neither surface can drift into reporting a different
music state than the other.

Held apart from ``ProgramStatus`` because the label is a music-surface word: the
status value object is deliberately format-neutral and reports a podcast segment
or audiobook chapter with the same fields.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Self, final

from punt_vox.types_programs.mode import Mode

if TYPE_CHECKING:
    from punt_vox.types_programs.status import ProgramStatus

__all__ = ["MusicStateView"]

type MusicMode = Literal["on", "off"]


@final
class MusicStateView:
    """The ``program`` block and its ``music_mode`` label, as one payload piece."""

    __slots__ = ("_mode", "_program")
    _program: dict[str, object]
    _mode: MusicMode

    def __new__(cls, program: dict[str, object], mode: MusicMode) -> Self:
        self = super().__new__(cls)
        self._program = program
        self._mode = mode
        return self

    @classmethod
    def of(cls, status: ProgramStatus) -> Self:
        """Project the daemon's authoritative status into the reported fields.

        ``music_mode`` is derived from the same status the ``program`` block
        carries, so a client stopping or starting music elsewhere can never leave
        the two contradicting each other.
        """
        return cls(status.to_dict(), "off" if status.mode is Mode.OFF else "on")

    @classmethod
    def unavailable(cls, reason: str) -> Self:
        """Report an unreachable daemon: the fault, and ``off``.

        Nothing can be confirmed playing when ``voxd`` cannot be asked, and the
        reason reaches the client through the payload rather than only a log.
        """
        return cls({"error": reason}, "off")

    def to_dict(self) -> dict[str, object]:
        """Return the two fields to merge into a status payload."""
        return {"program": self._program, "music_mode": self._mode}
