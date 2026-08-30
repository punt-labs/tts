"""Pins for hook-fire-to-store-receipt latency (bead b).

Both stamps ride the same record, so the pairing is per-record and immune
to arrival order -- these fixtures hold that, plus the ns->ms arithmetic
exactly, the per-event keying, and the visible (not silent) exclusion of
unstamped records.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from conftest import BASE_NS
from latency import LatencyReport

if TYPE_CHECKING:
    from conftest import RecordFactory

_MS = 1_000_000  # ns per millisecond


def _stats(report: LatencyReport, key: str) -> dict[str, float]:
    return cast("dict[str, float]", report.as_dict()[key])


def _per_event(report: LatencyReport, event: str) -> dict[str, float]:
    per_event = cast("dict[str, dict[str, float]]", report.as_dict()["per_event"])
    return per_event[event]


class TestPairing:
    """Fire and receipt stamps pair within one record, exactly."""

    def test_known_delta_is_reported_in_ms(self, record: RecordFactory) -> None:
        rec = record(
            event="Stop",
            relay_start_ns=BASE_NS,
            received_ns=BASE_NS + 5 * _MS,
        )
        report = LatencyReport((rec,))
        assert _per_event(report, "Stop")["p50"] == 5.0
        assert _per_event(report, "Stop")["max"] == 5.0

    def test_out_of_order_arrival_pairs_correctly(self, record: RecordFactory) -> None:
        # Fired second, arrived first (concurrent hook commands racing).
        # Each record carries its own start stamp, so the pairing cannot
        # cross records -- the latencies come out right regardless of order.
        late_fire = record(
            event="PostToolUse",
            relay_seq=2,
            relay_start_ns=BASE_NS + 100 * _MS,
            received_ns=BASE_NS + 103 * _MS,  # 3 ms
        )
        early_fire = record(
            event="PostToolUse",
            relay_seq=1,
            relay_start_ns=BASE_NS,
            received_ns=BASE_NS + 110 * _MS,  # 110 ms, arrived after
        )
        report = LatencyReport((late_fire, early_fire))
        stats = _per_event(report, "PostToolUse")
        assert stats["n"] == 2
        assert stats["p50"] == 3.0
        assert stats["max"] == 110.0

    def test_overall_aggregates_across_event_types(self, record: RecordFactory) -> None:
        records = tuple(
            record(
                event=event,
                relay_start_ns=BASE_NS,
                received_ns=BASE_NS + delta * _MS,
            )
            for event, delta in (("Stop", 10), ("PostToolUse", 20), ("Stop", 30))
        )
        report = LatencyReport(records)
        overall = _stats(report, "overall")
        assert overall["n"] == 3
        assert overall["p50"] == 20.0
        assert overall["max"] == 30.0


class TestUnstamped:
    """Records without a relay stamp are counted and excluded, not dropped."""

    def test_unstamped_records_are_counted(self, record: RecordFactory) -> None:
        records = (
            record(relay_start_ns=BASE_NS, received_ns=BASE_NS + _MS),
            record(),  # bypassed the wrapper: no relay_start_ns
            record(),
        )
        report = LatencyReport(records)
        assert report.as_dict()["unstamped_records"] == 2
        assert _stats(report, "overall")["n"] == 1

    def test_fully_unstamped_ledger_is_zeros_not_a_crash(
        self, record: RecordFactory
    ) -> None:
        report = LatencyReport((record(), record()))
        overall = _stats(report, "overall")
        assert overall["n"] == 0
        assert report.as_dict()["per_event"] == {}

    def test_table_names_the_exclusion(self, record: RecordFactory) -> None:
        report = LatencyReport((record(),))
        assert "1 records without relay stamp" in report.table()

    def test_empty_ledger_report_renders(self) -> None:
        report = LatencyReport(())
        assert _stats(report, "overall")["n"] == 0
        assert "overall latency_ms" in report.table()
