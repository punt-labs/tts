"""``ProgramStatus`` -- the first-class, format-spanning observability surface.

Because any client now drives ``voxd`` (ownership is gone), any client must be
able to *see* what ``voxd`` is doing. ``ProgramStatus`` is that view: a single,
format-neutral value object rendered identically through the ``mic`` MCP
``status`` tool and the ``vox music status`` CLI. It carries no ``vibe``/``style``
content (that is manifest *subject* data, not runtime status), so the same shape
reports a playlist track today and a podcast segment or audiobook chapter later
with no field change -- the shape is decided here, once (design section 5).

Both failure surfaces are present and distinct:
``generation.last_error`` is the *program-level* failure (retrying/failed,
nothing can play) and ``failed_parts`` is the *per-Part* permanent failure while
the Program plays on. Reading a log is not a strategy for a client: a caller
asking "what is playing?" gets exactly this, authoritatively, on every call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Self, final

from punt_vox.types_programs.format import Format
from punt_vox.types_programs.identifiers import ProgramName
from punt_vox.types_programs.mode import Mode, PlaybackStatus
from punt_vox.types_programs.playback_fault import PlaybackFault
from punt_vox.types_programs.status_views import (
    FailedPartView,
    GenerationStatus,
    NowPlaying,
)
from punt_vox.types_programs.wire import JsonObject

__all__ = ["ProgramStatus"]

_NO_GENERATION = GenerationStatus(filling=False, attempts=0)
"""The generation surface of an idle daemon -- nothing filling, no error."""


@final
@dataclass(frozen=True, slots=True)
class ProgramStatus:
    """The current Program's runtime status, regardless of format (design section 5).

    ``name is None`` means the daemon is idle (no active Program). An ``off``
    Program whose pool is saved on disk reports ``mode == off`` with ``name`` set
    and ``now_playing is None``, so a client tells "there is a pool to play" from
    "there is nothing".
    """

    format: Format
    mode: Mode
    generation: GenerationStatus
    name: ProgramName | None = None  # None means the daemon is idle (no active Program)
    now_playing: NowPlaying | None = None  # None when nothing is playing
    failed_parts: tuple[FailedPartView, ...] = field(default_factory=tuple)
    # None means the player is healthy; a fault means a Part could not be spawned
    # (missing afplay/ffplay, or an OS limit) -- observable, not a Program state.
    playback_error: PlaybackFault | None = None
    # True while the active source is suspended in place (transport pause); the
    # cursor is held and never auto-advances. Idle and generating are never paused.
    paused: bool = False

    @classmethod
    def idle(cls) -> Self:
        """Return the status of an idle daemon with no active Program."""
        return cls(format=Format.PLAYLIST, mode=Mode.OFF, generation=_NO_GENERATION)

    @classmethod
    def radio(
        cls,
        name: ProgramName | None,
        now_playing: NowPlaying | None,
        playback_error: PlaybackFault | None = None,
    ) -> Self:
        """Assemble the status of an active consume-only replay Selection.

        A replay generates nothing, so the generation surface is idle and the
        coarse mode is ``playing_rotating`` (a full, rotating pool). ``name`` is
        the replay's display handle; ``now_playing`` is the cursor's position. A
        radio track can still fault (a missing/corrupt file exits non-zero), so
        the daemon's live ``playback_error`` is surfaced here exactly as it is for
        a generate Program -- a replay fault is never invisible to a client.
        """
        return cls(
            format=Format.PLAYLIST,
            mode=Mode.PLAYING_ROTATING,
            generation=_NO_GENERATION,
            name=name,
            now_playing=now_playing,
            playback_error=playback_error,
        )

    @property
    def is_idle(self) -> bool:
        """Return whether no Program is active (the daemon holds nothing)."""
        return self.name is None

    @property
    def is_playing(self) -> bool:
        """Return whether a Part is audibly playing (the single audible gate)."""
        return self.mode.status is PlaybackStatus.PLAYING

    def summary(self) -> str:
        """Return the human render of this status: a headline plus any failures.

        Every surface that reports music state in prose -- the ``music`` MCP
        tool's ``status`` verb and ``vox music status`` -- prints this, so a
        caller who reads one is never told a different story by the other.
        """
        if self.name is None:
            return "Nothing playing."
        head = f"{self.name.value} [{self.format.label}]"
        return "\n".join(
            [
                f"{head} — {self._transport} ({self.mode.value})",
                *self.generation.error_lines(),
                *(part.line() for part in self.failed_parts),
            ]
        )

    @property
    def _transport(self) -> str:
        """Return the transport phrase -- a position, or ``stopped`` when silent."""
        now = self.now_playing
        return "stopped" if now is None else now.position(paused=self.paused)

    def to_dict(self) -> dict[str, object]:
        """Return the JSON object form -- the wire shape every client reads.

        An absent ``name`` (the idle daemon) is omitted, matching the codebase's
        omit-the-absent-optional convention; ``now_playing`` is an explicit
        ``null`` because a client branches on "playing vs not" every call.
        """
        record: dict[str, object] = {
            "format": self.format.value,
            "mode": self.mode.value,
            "now_playing": None
            if self.now_playing is None
            else self.now_playing.to_dict(),
            "generation": self.generation.to_dict(),
            "failed_parts": [view.to_dict() for view in self.failed_parts],
            "playback_error": None
            if self.playback_error is None
            else self.playback_error.to_dict(),
            "paused": self.paused,
        }
        if self.name is not None:
            record["name"] = self.name.value
        return record

    @classmethod
    def from_wire(cls, obj: JsonObject) -> Self:
        """Build a status from a wire object, raising on a malformed record."""
        now = obj.opt_object("now_playing")
        fault = obj.opt_object("playback_error")
        return cls(
            format=Format(obj.require_str("format")),
            mode=Mode(obj.require_str("mode")),
            generation=GenerationStatus.from_wire(obj.require_object("generation")),
            name=cls._name_of(obj.opt_str("name")),
            now_playing=None if now is None else NowPlaying.from_wire(now),
            failed_parts=tuple(
                FailedPartView.from_wire(JsonObject.coerce(item, "status.failed_parts"))
                for item in obj.require_list("failed_parts")
            ),
            playback_error=None if fault is None else PlaybackFault.from_wire(fault),
            paused=obj.opt_bool("paused") or False,
        )

    @staticmethod
    def _name_of(raw: str | None) -> ProgramName | None:
        """Return the Program name, or ``None`` for the idle daemon."""
        return None if raw is None else ProgramName(raw)
