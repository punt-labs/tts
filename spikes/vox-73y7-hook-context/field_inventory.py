# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""Per-hook-type field inventory: what each event carries, and how much.

Bead question (a): do hook payloads carry actionable STATE (tool inputs and
outputs, prompt text, file paths) or only bare metadata? This analyzer
reads a run's ledger and answers per event type: which payload fields
appear (and how often), which class each field falls into, and the
byte-size distribution of whole payloads and of the state-bearing share.

Field classes:

- ``state``   -- session work content itself: prompt text, tool inputs,
  tool responses, notification messages. This is what a rolling context
  store could feed a voice agent.
- ``pointer`` -- paths that point at state stored elsewhere on the host
  (the transcript file, the working directory). Reachable for a same-host
  voxd, gone for a remote one.
- ``metadata`` -- identity and plumbing: session id, event name, modes,
  the harness's own relay stamps.

Run:  uv run field_inventory.py --ledger <path> [--out <json>]
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Self, final

from percentile import PercentileStats
from stamp import HookLedger, HookRecord

_STATE_FIELDS = frozenset(
    {
        "prompt",
        "tool_input",
        "tool_response",
        "tool_name",
        "message",
        "title",
        # Stop events carry the assistant's final message for the turn --
        # discovered in the live capture; it is session work content, not
        # plumbing, and the reconstructor leans on it.
        "last_assistant_message",
    }
)
_POINTER_FIELDS = frozenset({"transcript_path", "cwd"})
# Everything else -- session_id, hook_event_name, permission_mode,
# stop_hook_active, source, reason, relay_seq, relay_start_ns, ... -- is
# metadata by exclusion; listing it exhaustively would rot as Claude Code
# adds fields, while the state list is the claim under test.


def _field_class(field: str) -> str:
    if field in _STATE_FIELDS:
        return "state"
    if field in _POINTER_FIELDS:
        return "pointer"
    return "metadata"


def _json_bytes(value: object) -> int:
    return len(json.dumps(value).encode("utf-8"))


@final
class EventProfile:
    """Accumulated field census and sizes for one hook event type."""

    __slots__ = ("_count", "_fields", "_payload_bytes", "_state_bytes")

    _count: int
    _fields: Counter[str]
    _payload_bytes: list[float]
    _state_bytes: list[float]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._count = 0
        self._fields = Counter()
        self._payload_bytes = []
        self._state_bytes = []
        return self

    def add(self, record: HookRecord) -> None:
        """Fold one record into the census."""
        self._count += 1
        self._fields.update(record.payload.keys())
        self._payload_bytes.append(float(_json_bytes(record.payload)))
        state_size = sum(
            _json_bytes(value)
            for field, value in record.payload.items()
            if _field_class(field) == "state"
        )
        self._state_bytes.append(float(state_size))

    def as_dict(self) -> dict[str, object]:
        """Machine-readable profile for the inventory JSON."""
        return {
            "count": self._count,
            "fields": {
                field: {
                    "class": _field_class(field),
                    "presence": round(occurrences / self._count, 3),
                }
                for field, occurrences in sorted(self._fields.items())
            },
            "payload_bytes": PercentileStats.of(self._payload_bytes).as_dict(),
            "state_bytes": PercentileStats.of(self._state_bytes).as_dict(),
        }

    def rows(self, event: str) -> list[str]:
        """Two aligned table rows: whole payload and state share."""
        payload = PercentileStats.of(self._payload_bytes)
        state = PercentileStats.of(self._state_bytes)
        return [
            payload.row(f"{event} payload_bytes"),
            state.row(f"{event} state_bytes"),
        ]


@final
class FieldInventory:
    """The whole-ledger inventory: one profile per event type."""

    __slots__ = ("_profiles",)

    _profiles: dict[str, EventProfile]

    def __new__(cls, records: tuple[HookRecord, ...]) -> Self:
        self = super().__new__(cls)
        profiles: defaultdict[str, EventProfile] = defaultdict(EventProfile)
        for record in records:
            profiles[record.event].add(record)
        self._profiles = dict(profiles)
        return self

    def as_dict(self) -> dict[str, object]:
        """The inventory JSON body, keyed by event type."""
        return {
            event: profile.as_dict()
            for event, profile in sorted(self._profiles.items())
        }

    def table(self) -> str:
        """Human-readable size table across all event types."""
        lines = [PercentileStats.header(), "-" * 74]
        for event in sorted(self._profiles):
            lines.extend(self._profiles[event].rows(event))
        return "\n".join(lines)


def main() -> None:
    """CLI entry: read a ledger, write the inventory JSON, print the table."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    inventory = FieldInventory(HookLedger(args.ledger).records())
    body = json.dumps(inventory.as_dict(), indent=2, sort_keys=True)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(body + "\n", encoding="utf-8")
    print(inventory.table())


if __name__ == "__main__":
    main()
