"""Monotonic sequence stamping and the JSONL ledger for relayed hook payloads.

This is the verdict-bearing core of the stub voxd context store: every hook
payload that arrives over the loopback relay is stamped with a global receive
sequence and a per-session sequence, attributed to the spawned session by its
``session_id``, and appended durably to a JSONL ledger. The rolling context
store described in DES-070 needs exactly these stamps; the spike proves they
can be produced from real `mcp-proxy --hook` traffic.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self, final

# Hook payloads cross the JSON-RPC wire verbatim -- a wire boundary, so the
# values stay untyped objects until a caller narrows a specific field.
type WirePayload = dict[str, object]

# Attribution fallback when a payload carries no usable session_id.
UNATTRIBUTED = "unattributed"

# Substrings that mark a payload key as credential-shaped. Values under such
# keys are replaced before the payload is persisted (DES-069 copy-forward:
# bearer-shaped material never lands in traces).
_REDACT_MARKERS = ("token", "secret", "signed_url", "api_key", "apikey")
_REDACTED = "[redacted]"


@final
@dataclass(frozen=True, slots=True)
class HookRecord:
    """One relayed hook payload, stamped with global and per-session order."""

    recv_seq: int
    session_seq: int
    session_id: str
    event: str
    received_at: str
    payload: WirePayload

    def to_json(self) -> str:
        """Serialize to one JSONL line."""
        return json.dumps(
            {
                "recv_seq": self.recv_seq,
                "session_seq": self.session_seq,
                "session_id": self.session_id,
                "event": self.event,
                "received_at": self.received_at,
                "payload": self.payload,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, line: str) -> Self:
        """Parse one JSONL line back into a record; raise on a bad shape."""
        raw = json.loads(line)
        if not isinstance(raw, dict):
            msg = f"ledger line is not an object: {line[:80]}"
            raise ValueError(msg)
        try:
            recv_seq = int(raw["recv_seq"])
            session_seq = int(raw["session_seq"])
            session_id = str(raw["session_id"])
            event = str(raw["event"])
            received_at = str(raw["received_at"])
            payload = raw["payload"]
        except KeyError as exc:
            msg = f"ledger line missing field {exc}"
            raise ValueError(msg) from exc
        if not isinstance(payload, dict):
            msg = "ledger payload is not an object"
            raise ValueError(msg)
        return cls(
            recv_seq=recv_seq,
            session_seq=session_seq,
            session_id=session_id,
            event=event,
            received_at=received_at,
            payload=payload,
        )


@final
class SequenceStamper:
    """Assigns the monotonic stamps DES-070's rolling context store needs."""

    __slots__ = ("_next_recv", "_per_session")

    _next_recv: int
    _per_session: dict[str, int]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._next_recv = 1
        self._per_session = {}
        return self

    def stamp(self, event: str, payload: WirePayload) -> HookRecord:
        """Stamp one payload: global order, per-session order, attribution."""
        session_id = self._attribute(payload)
        recv_seq = self._next_recv
        self._next_recv += 1
        session_seq = self._per_session.get(session_id, 0) + 1
        self._per_session[session_id] = session_seq
        return HookRecord(
            recv_seq=recv_seq,
            session_seq=session_seq,
            session_id=session_id,
            event=event,
            received_at=datetime.now(tz=UTC).isoformat(),
            payload=self._redacted(payload),
        )

    def _attribute(self, payload: WirePayload) -> str:
        raw = payload.get("session_id")
        if isinstance(raw, str) and raw:
            return raw
        return UNATTRIBUTED

    def _redacted(self, payload: WirePayload) -> WirePayload:
        # Recursive: hook payloads nest arbitrary structures under
        # tool_input / tool_response, and the ledger is a committed run
        # artifact -- a credential-shaped key at any depth must be masked
        # before persistence, not just at the top level.
        return {key: self._masked(key, value) for key, value in payload.items()}

    def _masked(self, key: str, value: object) -> object:
        if self._is_credential_key(key):
            return _REDACTED
        return self._scrubbed(value)

    def _scrubbed(self, value: object) -> object:
        if isinstance(value, dict):
            return {
                str(key): self._masked(str(key), item) for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._scrubbed(item) for item in value]
        return value

    def _is_credential_key(self, key: str) -> bool:
        lowered = key.lower()
        return any(marker in lowered for marker in _REDACT_MARKERS)


@final
class HookLedger:
    """Append-only JSONL file the stub store persists stamped records into.

    Each append opens, writes, flushes, and fsyncs so a SIGKILL of the store
    process (the survival test) cannot lose already-acknowledged records.
    """

    __slots__ = ("_path",)

    _path: Path

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        self._path = path
        return self

    @property
    def path(self) -> Path:
        """Where the JSONL ledger lives."""
        return self._path

    def append(self, record: HookRecord) -> None:
        """Durably append one record as a JSONL line."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(record.to_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def records(self) -> tuple[HookRecord, ...]:
        """Read every record back, in file order."""
        if not self._path.exists():
            return ()
        lines = self._path.read_text(encoding="utf-8").splitlines()
        return tuple(HookRecord.from_json(line) for line in lines if line)
