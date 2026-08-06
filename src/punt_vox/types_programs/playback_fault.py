"""The player-fault wire value types -- an observability surface, not a state.

A player fault is deliberately **not** a Z Program transition: the Part stays
ready and the playback cursor stays put, so the domain state machine is
untouched. But a client must still see that audio is not reaching the speakers,
so the loop or the mpv supervisor records the fault and
:class:`~punt_vox.types_programs.status.ProgramStatus` surfaces it. Reading a
daemon log is never a client interface.

Two families of fault share this surface, tagged by :class:`PlaybackFaultKind`:
a *per-part* fault (``TRACK_ERROR`` -- mpv reported a bad or corrupt part file),
and the *process-level* mpv faults that describe the one persistent player's
lifecycle -- ``PLAYER_UNAVAILABLE`` (mpv missing, too old, or never brought up),
``PLAYER_CRASH`` (mpv died and is being restarted), and ``PLAYER_FAILED`` (the
restart cap was exceeded). A process-level fault names no single part, so it
carries ``part_index == 0``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from punt_vox.types_programs.wire import JsonObject

__all__ = ["PlaybackFault", "PlaybackFaultKind"]

_PROCESS_LEVEL_INDEX = 0
"""``part_index`` for a process-level (not part-specific) mpv fault."""


class PlaybackFaultKind(StrEnum):
    """Which class of player fault a :class:`PlaybackFault` records."""

    TRACK_ERROR = "track_error"  # mpv reported a bad/corrupt part file (end-file error)
    PLAYER_UNAVAILABLE = "player_unavailable"  # mpv missing/too-old/never brought up
    PLAYER_CRASH = "player_crash"  # mpv died mid-playback; a restart is under way
    PLAYER_FAILED = "player_failed"  # the restart cap was exceeded; tier is dead


@final
@dataclass(frozen=True, slots=True)
class PlaybackFault:
    """A player fault: which Part it happened for, its kind, and why."""

    part_index: int  # the intrinsic index of the Part, or 0 for a process-level fault
    reason: str  # the human-readable diagnostic (the sanitized error text)
    kind: PlaybackFaultKind  # per-part track error vs a process-level mpv fault

    @classmethod
    def process_level(cls, reason: str, kind: PlaybackFaultKind) -> Self:
        """Build a process-level mpv fault, tied to no single part (index 0)."""
        return cls(part_index=_PROCESS_LEVEL_INDEX, reason=reason, kind=kind)

    def to_dict(self) -> dict[str, object]:
        """Return the JSON object form -- the wire shape a client reads."""
        return {
            "part_index": self.part_index,
            "reason": self.reason,
            "kind": self.kind.value,
        }

    @classmethod
    def from_wire(cls, obj: JsonObject) -> Self:
        """Build a fault from a wire object, raising on a malformed record."""
        return cls(
            part_index=obj.require_int("part_index"),
            reason=obj.require_str("reason"),
            kind=PlaybackFaultKind(obj.require_str("kind")),
        )
