# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""Per-event hook-fire-to-store-visible latency from a run's ledger.

Bead question (b): how long between a hook firing in the session and the
payload being visible in the store? Two clocks, one host: the relay shell
wrapper stamps ``relay_start_ns`` (wall clock, ``date +%s%N``) the moment
Claude Code starts the hook command, and the store stamps ``received_ns``
(wall clock, ``time.time_ns``) the moment the payload is accepted. The
difference is the whole delivery pipeline: shell + python wrapper startup,
sender-side stamping, mcp-proxy startup, the WebSocket dial, and the
store's own processing, measured on the same system clock.

Records without a ``relay_start_ns`` (payloads that bypassed the wrapper)
are counted and excluded, not silently dropped.

Run:  uv run latency.py --ledger <path> [--out <json>]
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Self, final

from percentile import PercentileStats
from stamp import HookLedger, HookRecord

_NS_PER_MS = 1_000_000.0


@final
class LatencyReport:
    """Delivery latencies per event type plus the overall aggregate."""

    __slots__ = ("_per_event", "_unstamped")

    _per_event: dict[str, list[float]]
    _unstamped: int

    def __new__(cls, records: tuple[HookRecord, ...]) -> Self:
        self = super().__new__(cls)
        per_event: defaultdict[str, list[float]] = defaultdict(list)
        unstamped = 0
        for record in records:
            start_ns = record.relay_start_ns()
            if start_ns is None:
                unstamped += 1
                continue
            per_event[record.event].append((record.received_ns - start_ns) / _NS_PER_MS)
        self._per_event = dict(per_event)
        self._unstamped = unstamped
        return self

    def _overall(self) -> list[float]:
        return [value for values in self._per_event.values() for value in values]

    def as_dict(self) -> dict[str, object]:
        """Machine-readable latency summary (milliseconds)."""
        return {
            "unit": "ms",
            "measured": "relay-script start (date +%s%N) to store receipt "
            "(time.time_ns), same host wall clock",
            "unstamped_records": self._unstamped,
            "overall": PercentileStats.of(self._overall()).as_dict(),
            "per_event": {
                event: PercentileStats.of(values).as_dict()
                for event, values in sorted(self._per_event.items())
            },
        }

    def table(self) -> str:
        """Human-readable latency table (milliseconds)."""
        lines = [PercentileStats.header(), "-" * 74]
        lines.append(PercentileStats.of(self._overall()).row("overall latency_ms"))
        lines.extend(
            PercentileStats.of(values).row(f"{event} latency_ms")
            for event, values in sorted(self._per_event.items())
        )
        if self._unstamped:
            lines.append(f"(excluded: {self._unstamped} records without relay stamp)")
        return "\n".join(lines)


def main() -> None:
    """CLI entry: read a ledger, write the latency JSON, print the table."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    if not args.ledger.exists():
        # Only the store treats an absent file as an empty ledger; an
        # analyzer doing so would report a clean run for a typo'd path.
        msg = f"ledger not found: {args.ledger}"
        raise SystemExit(msg)
    report = LatencyReport(HookLedger(args.ledger).records())
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(report.table())


if __name__ == "__main__":
    main()
