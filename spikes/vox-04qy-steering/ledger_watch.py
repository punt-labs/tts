"""Blocking receipt checks over the store's growing JSONL ledger.

Arm 2's delivery receipt is a ``UserPromptSubmit`` record appearing in the
hook ledger; this poller turns "appearing" into a blocking wait with a
deadline and a named condition, using the torn-line-tolerant snapshot read
(the store may be mid-append when we look).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Callable

    from stamp import HookLedger, HookRecord


def prompt_of(record: HookRecord) -> str:
    """The ``prompt`` field of a hook payload; empty when absent."""
    raw = record.payload.get("prompt")
    return raw if isinstance(raw, str) else ""


@final
class LedgerWatch:
    """Polls one ledger until a record satisfies a predicate."""

    __slots__ = ("_ledger", "_on_tick", "_poll_s")

    _ledger: HookLedger
    _on_tick: Callable[[], None] | None
    _poll_s: float

    def __new__(
        cls,
        ledger: HookLedger,
        poll_s: float = 0.5,
        on_tick: Callable[[], None] | None = None,  # None: nothing to nudge per poll
    ) -> Self:
        self = super().__new__(cls)
        self._ledger = ledger
        self._poll_s = poll_s
        self._on_tick = on_tick
        return self

    def records(self) -> tuple[HookRecord, ...]:
        """The complete records persisted so far (snapshot read)."""
        return self._ledger.records_snapshot()

    def count(self, event: str) -> int:
        """How many records of the given event type are persisted."""
        return sum(1 for record in self.records() if record.event == event)

    def wait_until(
        self, condition: Callable[[], bool], timeout_s: float, description: str
    ) -> None:
        """Block until ``condition()`` holds; raise on deadline.

        For ledger-level facts (an event count) rather than a single
        record; polls and nudges exactly like :meth:`wait_for`.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            if self._on_tick is not None:
                self._on_tick()
            if condition():
                return
            if time.monotonic() >= deadline:
                msg = f"condition never held within {timeout_s}s: {description}"
                raise TimeoutError(msg)
            time.sleep(self._poll_s)

    def wait_for(
        self,
        predicate: Callable[[HookRecord], bool],
        timeout_s: float,
        description: str,
    ) -> HookRecord:
        """Block until a persisted record matches; raise on deadline.

        ``on_tick`` runs once per poll — the harness uses it to nudge the
        fork's interactive dialogs while it waits.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            if self._on_tick is not None:
                self._on_tick()
            for record in self.records():
                if predicate(record):
                    return record
            if time.monotonic() >= deadline:
                msg = f"no ledger record matched within {timeout_s}s: {description}"
                raise TimeoutError(msg)
            time.sleep(self._poll_s)
