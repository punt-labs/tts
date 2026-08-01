"""The mpv JSON-IPC value types -- command, response, event, and end-file reason.

The program tier drives one persistent ``mpv`` process over its JSON IPC socket
(``docs/mpv-program-player.md``). Three value objects frame that wire, and one
enum names the ways a loaded part can end:

* :class:`MpvCommand` is an outbound command (``loadfile``/``set_property``/
  ``stop``/``quit``); it serialises itself with a caller-assigned ``request_id``.
* :class:`MpvResponse` is the reply mpv sends for a command bearing that id.
* :class:`MpvEvent` is an unsolicited event (the one that matters is
  ``end-file``, discriminated by its :class:`EndFileReason`).
* :class:`EndFileReason` is the mpv ``end-file`` reason, plus the synthetic
  ``crashed`` the reader injects on socket EOF (a crash emits no ``end-file``),
  so the one channel the loop awaits carries every way a part can end.

These are pure value types (PY-IC-9): no socket, no I/O, no state machine. The
connection that uses them lives in ``voxd/programs/mpv/``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Self, final

from punt_vox.types_programs.end_file_reason import EndFileReason
from punt_vox.types_programs.wire import JsonObject

__all__ = [
    "EndFileReason",
    "MpvArg",
    "MpvCommand",
    "MpvEvent",
    "MpvResponse",
]

type MpvArg = str | int | bool
"""A single element of an mpv command array -- a string, int, or JSON bool."""

_LOAD_REPLACE = "replace"
_SUCCESS = "success"


@final
@dataclass(frozen=True, slots=True)
class MpvCommand:
    """An outbound mpv IPC command, framed with a caller-assigned ``request_id``."""

    args: tuple[MpvArg, ...]

    @classmethod
    def loadfile(cls, path: str) -> Self:
        """Return a ``loadfile <path> replace`` command -- play ``path`` now.

        Whether the loaded part starts paused is carried by mpv's global
        ``pause`` property, not a per-file option: the player sets ``pause`` to
        match the suspension flag *before* this load (Fork B), so a prev/next or
        post-crash reload while paused loads paused without depending on a newer
        mpv's ``loadfile`` options argument. Keeping the command to the
        three-element form holds the IPC contract at the pinned minimum version.
        """
        return cls(("loadfile", path, _LOAD_REPLACE))

    @classmethod
    def set_pause(cls, *, paused: bool) -> Self:
        """Return a ``set_property pause <bool>`` command (click-free suspend)."""
        return cls(("set_property", "pause", paused))

    @classmethod
    def stop(cls) -> Self:
        """Return a ``stop`` command -- unload the current file, return to idle."""
        return cls(("stop",))

    @classmethod
    def quit(cls) -> Self:
        """Return a ``quit`` command -- ask mpv to exit gracefully (shutdown)."""
        return cls(("quit",))

    def framed(self, request_id: int) -> bytes:
        """Return the newline-terminated JSON frame for the wire."""
        payload = {"command": list(self.args), "request_id": request_id}
        return (json.dumps(payload) + "\n").encode()


@final
@dataclass(frozen=True, slots=True)
class MpvResponse:
    """mpv's reply to a command: the request id it answers and its status."""

    request_id: int
    error: str

    @property
    def ok(self) -> bool:
        """Return whether mpv accepted the command (``error == "success"``)."""
        return self.error == _SUCCESS

    @classmethod
    def from_object(cls, obj: JsonObject) -> Self:
        """Build a response from a parsed reply object, raising if malformed."""
        return cls(
            request_id=obj.require_int("request_id"),
            error=obj.require_str("error"),
        )


@final
@dataclass(frozen=True, slots=True)
class MpvEvent:
    """An unsolicited mpv event; ``reason`` is set only for ``end-file``."""

    name: str
    reason: EndFileReason | None  # the end-file discriminant; None for other events

    @classmethod
    def from_object(cls, obj: JsonObject) -> Self:
        """Build an event from a parsed event object, raising if malformed.

        An ``end-file`` carries a ``reason``; every other event has none. An
        unknown reason value raises (the reader logs and skips), so a malformed
        event never resolves the loop's ended-future with a bogus outcome.
        """
        name = obj.require_str("event")
        if name != "end-file":
            return cls(name=name, reason=None)
        return cls(name=name, reason=EndFileReason(obj.require_str("reason")))
