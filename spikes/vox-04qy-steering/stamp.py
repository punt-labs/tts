"""Monotonic sequence stamping and the JSONL ledger for relayed hook payloads.

Copied verbatim from the frozen vox-73y7 spike's ``stamp.py`` (itself the
hardened vox-juhw core: recursive credential redaction, host path
sanitization, torn-line-tolerant snapshot reads). This spike reuses it
unchanged: the ``received_ns`` receipt stamp is the store side of the
send-to-hook-visible latency measurement, and the ``Sanitizer`` scrubs
every committed evidence artifact for BOTH arms before it lands in git.
"""

from __future__ import annotations

import getpass
import json
import os
import time
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
# bearer-shaped material never lands in traces). Matching is on the LOWERED
# key, and both underscore and hyphen spellings are listed because real tool
# inputs carry HTTP-header shapes (X-Api-Key, Authorization) that the
# underscore-only set let through.
_REDACT_MARKERS = (
    "token",
    "secret",
    "signed_url",
    "signed-url",
    "api_key",
    "api-key",
    "apikey",
    "access_key",
    "access-key",
    "authorization",
    "password",
    "credential",
)
_REDACTED = "[redacted]"


@final
class Sanitizer:
    """Rewrites host-specific path prefixes to stable placeholders.

    Ledgers and captures are committed run artifacts; absolute paths in
    them leak the username and machine layout. Rules apply in order, so
    the more specific prefix (the scratch root, which lives under the
    home dir) must precede the general one.
    """

    __slots__ = ("_rules",)

    _rules: tuple[tuple[str, str], ...]

    def __new__(cls, rules: tuple[tuple[str, str], ...]) -> Self:
        self = super().__new__(cls)
        self._rules = rules
        return self

    @classmethod
    def for_host(cls, scratch_root: Path | None = None) -> Self:
        """Standard host rules: scratch root -> <scratch>, home -> ~.

        ``scratch_root`` is optional because callers outside a harness
        run (unit tests, ad-hoc store starts) have no scratch namespace.
        Each prefix is also matched in Claude Code's dash-encoded form
        (the ``projects/`` directory slug, ``/`` and ``.`` -> ``-``),
        which otherwise re-leaks the username through transcript paths.
        The bare username is scrubbed LAST: path rules must run first or
        the username hit inside them would break the prefix match --
        found when an ``ls -la`` owner column re-leaked it.
        """
        rules: list[tuple[str, str]] = []
        if scratch_root is not None:
            rules.append((str(scratch_root), "<scratch>"))
            rules.append((cls._dash_encoded(str(scratch_root)), "<scratch-slug>"))
        rules.append((str(Path.home()), "~"))
        rules.append((cls._dash_encoded(str(Path.home())), "<home-slug>"))
        rules.append((getpass.getuser(), "<user>"))
        return cls(tuple(rules))

    @staticmethod
    def _dash_encoded(prefix: str) -> str:
        return prefix.replace("/", "-").replace(".", "-")

    @classmethod
    def null(cls) -> Self:
        """A sanitizer with no rules -- text passes through untouched."""
        return cls(())

    def scrub(self, text: str) -> str:
        """Apply every rule to ``text``, in order."""
        for prefix, placeholder in self._rules:
            text = text.replace(prefix, placeholder)
        return text


@final
@dataclass(frozen=True, slots=True)
class HookRecord:
    """One relayed hook payload, stamped with global and per-session order."""

    recv_seq: int
    session_seq: int
    session_id: str
    event: str
    received_at: str
    received_ns: int
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
                "received_ns": self.received_ns,
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
            received_ns = int(raw["received_ns"])
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
            received_ns=received_ns,
            payload=payload,
        )

    def relay_start_ns(self) -> int | None:
        # None when the payload skipped the sender-side stamping wrapper
        # (e.g. a bare mcp-proxy relay); latency is computable only for
        # wrapper-stamped events, so the absence is data, not an error.
        # bool is excluded explicitly: it subclasses int, so a JSON `true`
        # would otherwise read as the number 1.
        raw = self.payload.get("relay_start_ns")
        if isinstance(raw, int) and not isinstance(raw, bool):
            return raw
        return None

    def relay_seq(self) -> int | None:
        # None for the same reason as relay_start_ns: only wrapper-stamped
        # payloads carry the sender-side sequence gap detection needs.
        raw = self.payload.get("relay_seq")
        if isinstance(raw, int) and not isinstance(raw, bool):
            return raw
        return None


@final
class SequenceStamper:
    """Assigns the monotonic stamps DES-070's rolling context store needs."""

    __slots__ = ("_next_recv", "_per_session", "_sanitizer")

    _next_recv: int
    _per_session: dict[str, int]
    _sanitizer: Sanitizer

    def __new__(cls, sanitizer: Sanitizer | None = None) -> Self:
        # sanitizer is optional: unit tests exercising pure stamping pass
        # none and get pass-through text.
        self = super().__new__(cls)
        self._next_recv = 1
        self._per_session = {}
        self._sanitizer = sanitizer if sanitizer is not None else Sanitizer.null()
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
            received_ns=time.time_ns(),
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
        if isinstance(value, str):
            return self._sanitizer.scrub(value)
        return value

    def _is_credential_key(self, key: str) -> bool:
        lowered = key.lower()
        return any(marker in lowered for marker in _REDACT_MARKERS)


@final
class HookLedger:
    """Append-only JSONL file the stub store persists stamped records into.

    Each append opens, writes, flushes, and fsyncs so a SIGKILL of the store
    process (the gap-detection test) cannot lose already-acknowledged
    records.
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
        """Read every record back, in file order. Strict: a torn final
        line raises -- at judgment time (store dead, file closed) a
        fragment is real corruption, never a write in progress."""
        if not self._path.exists():
            return ()
        raw = self._path.read_text(encoding="utf-8")
        return tuple(HookRecord.from_json(line) for line in raw.splitlines() if line)

    def records_snapshot(self) -> tuple[HookRecord, ...]:
        """Read complete records, tolerating ONE in-flight final line.

        A concurrent reader (the harness poll loop) can catch the store
        mid-append: the file then ends in a fragment with no trailing
        newline. Every completed append ends in a newline, so exactly
        that unterminated tail is skipped; a malformed line anywhere in
        the terminated portion still raises like :meth:`records`.
        """
        if not self._path.exists():
            return ()
        raw = self._path.read_text(encoding="utf-8")
        terminated, _, _in_flight = raw.rpartition("\n")
        return tuple(
            HookRecord.from_json(line) for line in terminated.splitlines() if line
        )
