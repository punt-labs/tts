"""pi RPC wire objects: commands out, JSONL events in, a stamped transcript.

The wire contract (confirmed live against pi 0.84.4, and matching DES-066's
protocol evidence): one JSON object per line on stdin —
``{"type":"prompt"|"steer"|"follow_up","message":...}`` or
``{"type":"abort"}`` — and one JSON object per line on stdout, each carrying
a ``type`` (``response``, ``queue_update``, ``agent_start``,
``message_update``, ``agent_end``, ...). Event payload schemas beyond
``type`` are deliberately NOT modeled: the spike characterizes them, so the
event keeps its raw line and answers substring probes instead of committing
to shapes the evidence is supposed to discover.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from pathlib import Path

    from stamp import Sanitizer

# Wire payloads stay untyped objects until a caller narrows a field.
type WireObject = dict[str, object]


@final
@dataclass(frozen=True, slots=True)
class RpcCommand:
    """One command line for pi's RPC stdin."""

    command_type: str
    # None only for message-less commands (abort); the named constructors
    # are the boundary that enforces non-empty text everywhere else.
    message: str | None

    @classmethod
    def prompt(cls, text: str) -> Self:
        """A new user turn."""
        return cls._with_message("prompt", text)

    @classmethod
    def steer(cls, text: str) -> Self:
        """Mid-turn steering input — the verb under test."""
        return cls._with_message("steer", text)

    @classmethod
    def follow_up(cls, text: str) -> Self:
        """A queued next turn — the contrast case for steer."""
        return cls._with_message("follow_up", text)

    @classmethod
    def abort(cls) -> Self:
        """Stop the current turn."""
        return cls(command_type="abort", message=None)

    @classmethod
    def _with_message(cls, command_type: str, text: str) -> Self:
        if not text:
            msg = f"refusing to send an empty {command_type}"
            raise ValueError(msg)
        return cls(command_type=command_type, message=text)

    def to_wire(self) -> str:
        """The single JSON line pi reads for this command."""
        body: WireObject = {"type": self.command_type}
        if self.message is not None:
            body["message"] = self.message
        return json.dumps(body)


@final
class RpcEvent:
    """One parsed stdout line, stamped with its receipt nanosecond."""

    __slots__ = ("_data", "_raw", "_recv_ns")

    _data: WireObject
    _raw: str
    _recv_ns: int

    def __new__(cls, line: str, recv_ns: int) -> Self:
        parsed = json.loads(line)
        if not isinstance(parsed, dict):
            msg = f"RPC event line is not an object: {line[:80]}"
            raise ValueError(msg)
        event_type = parsed.get("type")
        if not isinstance(event_type, str) or not event_type:
            msg = f"RPC event line has no type: {line[:80]}"
            raise ValueError(msg)
        self = super().__new__(cls)
        self._raw = line
        self._recv_ns = recv_ns
        self._data = parsed
        return self

    @property
    def type(self) -> str:
        """The event's ``type`` field."""
        return str(self._data["type"])

    @property
    def recv_ns(self) -> int:
        """Wall-clock nanoseconds when the line was read."""
        return self._recv_ns

    @property
    def data(self) -> WireObject:
        """The parsed event object."""
        return self._data

    def is_response_to(self, command_type: str) -> bool:
        """True for the ack/nack of the named command."""
        return self.type == "response" and self._data.get("command") == command_type

    def contains(self, needle: str) -> bool:
        """Substring probe over the raw line.

        Marker detection (STEERED-ACK and friends) must not depend on the
        exact nesting of message content — the schema is a spike finding,
        not an input.
        """
        return needle in self._raw


@final
@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    """One stamped line, either direction."""

    direction: str
    ns: int
    text: str


@final
class Transcript:
    """The ordered, stamped in/out log a scenario's evidence is written from."""

    __slots__ = ("_entries",)

    _entries: list[TranscriptEntry]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._entries = []
        return self

    def note_send(self, wire: str, ns: int) -> None:
        """Record one command line written to pi's stdin."""
        self._entries.append(TranscriptEntry(direction="send", ns=ns, text=wire))

    def note_recv(self, line: str, ns: int) -> None:
        """Record one event line read from pi's stdout."""
        self._entries.append(TranscriptEntry(direction="recv", ns=ns, text=line))

    def entries(self) -> tuple[TranscriptEntry, ...]:
        """Every entry, in wire order."""
        return tuple(self._entries)

    def events(self) -> tuple[RpcEvent, ...]:
        """The received lines, parsed, in wire order."""
        return tuple(
            RpcEvent(entry.text, entry.ns)
            for entry in self._entries
            if entry.direction == "recv"
        )

    def dump(self, path: Path, sanitizer: Sanitizer) -> None:
        """Write the transcript as sanitized JSONL evidence."""
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = (
            json.dumps(
                {
                    "dir": entry.direction,
                    "ns": entry.ns,
                    "data": sanitizer.scrub(entry.text),
                }
            )
            for entry in self._entries
        )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
