# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""Gap detection over the sender-side per-session relay sequence.

Bead question (c): when the store dies mid-session and events are lost,
can sequence numbers detect and quantify the gap on resume? The answer
hinges on WHO assigns the sequence:

- The store's own ``session_seq`` (receiver-side, as DES-070 currently
  words it) CANNOT detect loss: events lost while the store was down were
  never received, so receiver sequences stay contiguous by construction --
  and a store restart resets them to 1, which reads as a reset, not a gap.
- The relay wrapper's ``relay_seq`` (sender-side, incremented at every
  hook fire by a counter the launcher owns) CAN: after resume, the set of
  received ``relay_seq`` values has holes exactly where the dead-store
  window was, and the hole count is the number of lost events.

This analyzer reports both, so the run's evidence shows the contrast, and
quantifies the loss window per session. Out-of-order arrival (concurrent
hook commands racing) is not loss: only values missing below the observed
maximum count as gaps.

Known blind spot, inherent to sender-sequence detection: TRAILING losses
-- events lost after the highest sequence that was ever received, with
nothing arriving behind them -- are invisible, because no later arrival
exposes the hole. Detection covers interior losses only; a consumer that
must also catch tail loss needs an end-of-session handshake (or accepts
the blind spot).

Run:  uv run gap_check.py --ledger <path> [--out <json>]
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Self, final

from stamp import HookLedger, HookRecord


@final
class SessionGapReport:
    """Gap analysis for one session's records, in receipt order."""

    __slots__ = ("_records",)

    _records: tuple[HookRecord, ...]

    def __new__(cls, records: tuple[HookRecord, ...]) -> Self:
        self = super().__new__(cls)
        self._records = records
        return self

    def missing_relay_seqs(self) -> tuple[int, ...]:
        """Sender-side sequence values that never arrived (the lost events)."""
        seen = {seq for r in self._records if (seq := r.relay_seq()) is not None}
        if not seen:
            return ()
        return tuple(sorted(set(range(1, max(seen) + 1)) - seen))

    def receiver_seq_resets(self) -> int:
        """How many times session_seq restarted (a store restart artifact).

        A reset is a session_seq that fails to increase over its
        predecessor in receipt order. Contiguity between resets is the
        point being demonstrated: receiver sequences carry no loss signal.
        """
        resets = 0
        previous = 0
        for record in self._records:
            if record.session_seq <= previous:
                resets += 1
            previous = record.session_seq
        return resets

    def as_dict(self) -> dict[str, object]:
        """Machine-readable per-session gap summary."""
        missing = self.missing_relay_seqs()
        stamped = [r for r in self._records if r.relay_seq() is not None]
        return {
            "received": len(self._records),
            "relay_stamped": len(stamped),
            "lost_events": len(missing),
            "missing_relay_seqs": list(missing),
            "receiver_seq_resets": self.receiver_seq_resets(),
            "gap_detected": bool(missing),
        }


@final
class GapReport:
    """Whole-ledger gap analysis, keyed by session."""

    __slots__ = ("_sessions",)

    _sessions: dict[str, SessionGapReport]

    def __new__(cls, records: tuple[HookRecord, ...]) -> Self:
        self = super().__new__(cls)
        by_session: defaultdict[str, list[HookRecord]] = defaultdict(list)
        for record in records:
            by_session[record.session_id].append(record)
        self._sessions = {
            session: SessionGapReport(tuple(session_records))
            for session, session_records in by_session.items()
        }
        return self

    def as_dict(self) -> dict[str, object]:
        """Machine-readable report body."""
        return {
            "sessions": {
                session: report.as_dict()
                for session, report in sorted(self._sessions.items())
            }
        }

    def summary(self) -> str:
        """One line per session for the console.

        ``relay_stamped`` is printed deliberately: a session whose
        events all lack sender stamps reports zero gaps VACUOUSLY, and
        that must be visible, not silent.
        """
        lines = []
        for session, report in sorted(self._sessions.items()):
            body = report.as_dict()
            lines.append(
                f"{session}: received={body['received']} "
                f"relay_stamped={body['relay_stamped']} "
                f"lost={body['lost_events']} "
                f"receiver_resets={body['receiver_seq_resets']} "
                f"gap_detected={body['gap_detected']}"
            )
        return "\n".join(lines)


def main() -> None:
    """CLI entry: read a ledger, write the gap JSON, print the summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    if not args.ledger.exists():
        # Only the store treats an absent file as an empty ledger; an
        # analyzer doing so would report a clean run for a typo'd path.
        msg = f"ledger not found: {args.ledger}"
        raise SystemExit(msg)
    report = GapReport(HookLedger(args.ledger).records())
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(report.summary())


if __name__ == "__main__":
    main()
