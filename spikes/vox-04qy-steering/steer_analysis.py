"""Transcript analysis: the latencies and timelines the Arm 1 verdict cites.

Pure arithmetic over a finished :class:`~rpc_protocol.Transcript` — no
process, no clock. The runner records; this derives.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Self, final

from rpc_protocol import RpcEvent

if TYPE_CHECKING:
    from collections.abc import Callable

    from rpc_protocol import Transcript, TranscriptEntry

_NS_PER_MS = 1_000_000


@final
class TranscriptAnalysis:
    """Answers the timing questions a scenario's transcript poses."""

    __slots__ = ("_entries", "_events")

    _entries: tuple[TranscriptEntry, ...]
    _events: tuple[RpcEvent, ...]

    def __new__(cls, transcript: Transcript) -> Self:
        self = super().__new__(cls)
        self._entries = transcript.entries()
        self._events = transcript.events()
        return self

    def send_ns(self, command_type: str) -> int:
        """The send stamp of the first command of the given type."""
        for entry in self._entries:
            if entry.direction != "send":
                continue
            body = json.loads(entry.text)
            if body.get("type") == command_type:
                return entry.ns
        msg = f"no {command_type} command in the transcript"
        raise LookupError(msg)

    def first_event_after(
        self,
        after_ns: int,
        predicate: Callable[[RpcEvent], bool],
        description: str,
    ) -> RpcEvent:
        """The first received event past ``after_ns`` matching ``predicate``."""
        for event in self._events:
            if event.recv_ns > after_ns and predicate(event):
                return event
        msg = f"no event after {after_ns}ns matched: {description}"
        raise LookupError(msg)

    @staticmethod
    def elapsed_ms(from_ns: int, to_ns: int) -> float:
        """Nanosecond difference as milliseconds."""
        return (to_ns - from_ns) / _NS_PER_MS

    def timeline(self) -> tuple[dict[str, object], ...]:
        """Every entry as ``{ms, dir, label}`` offsets from the first entry.

        The label is the command type for sends and the event type for
        receives — the summary file's human-scannable spine.
        """
        if not self._entries:
            return ()
        origin = self._entries[0].ns
        return tuple(
            {
                "ms": self.elapsed_ms(origin, entry.ns),
                "dir": entry.direction,
                "label": self._label(entry),
            }
            for entry in self._entries
        )

    @staticmethod
    def _label(entry: TranscriptEntry) -> str:
        try:
            body = json.loads(entry.text)
        except ValueError:
            # A stray non-JSON stdout line the session layer tolerated;
            # it keeps its slot in the timeline, just unlabeled.
            return "?"
        label = body.get("type") if isinstance(body, dict) else None
        return label if isinstance(label, str) else "?"
