"""The format-neutral sub-views of a Program's runtime status.

Three small value objects project the parts of a :class:`ProgramState` a client
watches -- what is playing, how generation is faring, and which Parts failed
permanently. Every field is format-agnostic: "Part N of M" reads the same for a
playlist track, podcast segment, or audiobook chapter. Each round-trips through
JSON so the value crosses the ``voxd`` wire to any client (PY-EH-8 on deserialize).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self, final

from punt_vox.types_programs.wire import JsonObject

__all__ = ["FailedPartView", "GenerationStatus", "NowPlaying"]


@final
@dataclass(frozen=True, slots=True)
class NowPlaying:
    """The playing Part's position in the ordered pool -- "Part ``index`` of ``of``".

    ``index`` is the 1-based *position* of the playing Part in the ordered pool and
    ``of`` is the pool's size, so ``index <= of`` always holds -- it is a
    position-of-count, not an intrinsic track number, so a gapped pool never
    reports a nonsensical "4 of 3".
    """

    index: int  # 1-based position of the playing Part in the ordered pool
    of: int  # total Parts currently in the pool (the "M" in "N of M")

    def position(self, *, paused: bool) -> str:
        """Return ``playing N of M``, or ``paused N of M`` while held.

        A suspended source is not advancing, so reporting it as playing would
        tell a caller the wrong story about a Program that has stopped moving.
        """
        verb = "paused" if paused else "playing"
        return f"{verb} {self.index} of {self.of}"

    def to_dict(self) -> dict[str, object]:
        """Return the JSON object form."""
        return {"index": self.index, "of": self.of}

    @classmethod
    def from_wire(cls, obj: JsonObject) -> Self:
        """Build a now-playing view from a wire object, raising on a bad record."""
        return cls(index=obj.require_int("index"), of=obj.require_int("of"))


@final
@dataclass(frozen=True, slots=True)
class GenerationStatus:
    """The program-level generation/error surface.

    ``last_error`` is the *program-level* advisory error (set while retrying or
    failed, ``None`` when healthy) -- distinct from the per-Part failures in
    :class:`FailedPartView`. ``None`` is the documented "healthy" contract, not a
    gave-up sentinel (PY-TS-14).
    """

    filling: bool  # a background fill is running
    attempts: int  # transient retries in flight (0 unless retrying)
    last_error: str | None = None  # program-level error text; None when healthy

    def error_lines(self) -> tuple[str, ...]:
        """Return the indented error line for a human summary, empty when healthy."""
        if self.last_error is None:
            return ()
        return (f"  error: {self.last_error}",)

    def to_dict(self) -> dict[str, object]:
        """Return the JSON object form, omitting an absent error."""
        record: dict[str, object] = {"filling": self.filling, "attempts": self.attempts}
        if self.last_error is not None:
            record["last_error"] = self.last_error
        return record

    @classmethod
    def from_wire(cls, obj: JsonObject) -> Self:
        """Build a generation view from a wire object, raising on a bad record."""
        return cls(
            filling=obj.require_bool("filling"),
            attempts=obj.require_int("attempts"),
            last_error=obj.opt_str("last_error"),
        )


@final
@dataclass(frozen=True, slots=True)
class FailedPartView:
    """One permanently-failed Part while the Program plays on."""

    index: int  # the intrinsic index of a Part that hit a permanent error
    reason: str  # the human-readable failure diagnostic

    def line(self) -> str:
        """Return the indented failure line for a human summary."""
        return f"  part {self.index} failed: {self.reason}"

    def to_dict(self) -> dict[str, object]:
        """Return the JSON object form."""
        return {"index": self.index, "reason": self.reason}

    @classmethod
    def from_wire(cls, obj: JsonObject) -> Self:
        """Build a failed-Part view from a wire object, raising on a bad record."""
        return cls(index=obj.require_int("index"), reason=obj.require_str("reason"))
