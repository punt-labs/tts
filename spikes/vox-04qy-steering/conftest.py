"""Shared fixture: a HookRecord factory with controllable stamps.

The analyzers (inventory, gaps, latency, context) all consume
``HookRecord`` tuples; building them through ``SequenceStamper`` would tie
every fixture to wall-clock time and hide the sequence arithmetic under
test. This factory constructs records directly, with every stamp explicit
and deterministic.
"""

from __future__ import annotations

from typing import final

import pytest

from stamp import HookRecord, WirePayload

# A plausible time_ns() origin for fixtures; offsets are added per record.
BASE_NS = 1_780_000_000_000_000_000


@final
class RecordFactory:
    """Builds deterministic HookRecords, auto-advancing the sequences."""

    __slots__ = ("_next_recv", "_next_session_seq")

    _next_recv: int
    _next_session_seq: dict[str, int]

    def __new__(cls) -> RecordFactory:
        self = super().__new__(cls)
        self._next_recv = 1
        self._next_session_seq = {}
        return self

    def __call__(
        self,
        event: str = "PostToolUse",
        session_id: str = "sess-1",
        payload: WirePayload | None = None,
        received_ns: int | None = None,
        relay_seq: int | None = None,
        relay_start_ns: int | None = None,
    ) -> HookRecord:
        """One record; sequences advance like the real stamper's."""
        recv_seq = self._next_recv
        self._next_recv += 1
        session_seq = self._next_session_seq.get(session_id, 0) + 1
        self._next_session_seq[session_id] = session_seq
        body: WirePayload = {"session_id": session_id, "hook_event_name": event}
        if payload is not None:
            body.update(payload)
        if relay_seq is not None:
            body["relay_seq"] = relay_seq
        if relay_start_ns is not None:
            body["relay_start_ns"] = relay_start_ns
        return HookRecord(
            recv_seq=recv_seq,
            session_seq=session_seq,
            session_id=session_id,
            event=event,
            received_at=f"2026-08-30T00:00:{recv_seq:02d}+00:00",
            received_ns=received_ns if received_ns is not None else BASE_NS + recv_seq,
            payload=body,
        )


@pytest.fixture
def record() -> RecordFactory:
    """A fresh factory per test."""
    return RecordFactory()
