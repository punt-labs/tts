"""Pins for gap detection over the sender-side relay sequence (bead c).

The verdict's loss claim is exact arithmetic: holes below the observed
maximum relay_seq, counted per session. These fixtures hold that a missing
range is detected and quantified exactly, that a clean ledger reports no
gap, that out-of-order arrival is not loss, and that the receiver-side
session_seq demonstrably carries no loss signal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from conftest import RecordFactory
from gap_check import GapReport, SessionGapReport

if TYPE_CHECKING:
    from stamp import HookRecord


def _session_body(report: GapReport, session: str) -> dict[str, object]:
    sessions = cast("dict[str, dict[str, object]]", report.as_dict()["sessions"])
    return sessions[session]


def _with_relay_seqs(
    record: RecordFactory, seqs: tuple[int, ...]
) -> tuple[HookRecord, ...]:
    return tuple(record(event="PostToolUse", relay_seq=seq) for seq in seqs)


class TestMissingRelaySeqs:
    """Holes below the observed maximum, exactly."""

    def test_clean_ledger_has_no_gap(self, record: RecordFactory) -> None:
        report = SessionGapReport(_with_relay_seqs(record, (1, 2, 3, 4, 5)))
        assert report.missing_relay_seqs() == ()
        assert report.as_dict()["gap_detected"] is False
        assert report.as_dict()["lost_events"] == 0

    def test_missing_range_is_detected_and_quantified_exactly(
        self, record: RecordFactory
    ) -> None:
        # Store dead while sender fired 4, 5, 6: exactly those three.
        report = SessionGapReport(_with_relay_seqs(record, (1, 2, 3, 7, 8)))
        assert report.missing_relay_seqs() == (4, 5, 6)
        body = report.as_dict()
        assert body["lost_events"] == 3
        assert body["gap_detected"] is True

    def test_out_of_order_arrival_is_not_loss(self, record: RecordFactory) -> None:
        # Concurrent hook commands race: 2 lands before 1. Nothing is lost.
        report = SessionGapReport(_with_relay_seqs(record, (2, 1, 3)))
        assert report.missing_relay_seqs() == ()

    def test_no_phantom_gap_above_the_observed_maximum(
        self, record: RecordFactory
    ) -> None:
        # Events lost AFTER the last received one are invisible to this
        # check by design: there is no observed maximum to count against.
        report = SessionGapReport(_with_relay_seqs(record, (1, 2, 3)))
        assert report.missing_relay_seqs() == ()

    def test_unstamped_ledger_reports_nothing(self, record: RecordFactory) -> None:
        # No relay stamps at all (wrapper bypassed): the analyzer must
        # report zero stamped records rather than invent a gap.
        report = SessionGapReport((record(), record()))
        body = report.as_dict()
        assert body["relay_stamped"] == 0
        assert body["missing_relay_seqs"] == []
        assert body["gap_detected"] is False

    def test_unstamped_records_do_not_disturb_stamped_arithmetic(
        self, record: RecordFactory
    ) -> None:
        records = (
            record(relay_seq=1),
            record(),  # e.g. a payload that skipped the wrapper
            record(relay_seq=3),
        )
        report = SessionGapReport(records)
        assert report.missing_relay_seqs() == (2,)
        assert report.as_dict()["relay_stamped"] == 2


class TestReceiverSeqResets:
    """session_seq restarts read as resets, never as gaps."""

    def test_contiguous_receiver_seqs_have_zero_resets(
        self, record: RecordFactory
    ) -> None:
        report = SessionGapReport(tuple(record() for _ in range(5)))
        assert report.receiver_seq_resets() == 0

    def test_store_restart_reads_as_one_reset_and_no_gap(self) -> None:
        # Two stamper lifetimes over one session: 1,2,3 then 1,2. The
        # receiver sequence resets once and stays contiguous -- the
        # demonstration that it carries no loss signal.
        before = RecordFactory()
        after = RecordFactory()
        records = tuple([before() for _ in range(3)] + [after() for _ in range(2)])
        report = SessionGapReport(records)
        assert report.receiver_seq_resets() == 1
        assert report.missing_relay_seqs() == ()  # receiver side sees no hole


class TestPerSessionKeying:
    """Gaps are quantified per session, never across sessions."""

    def test_sessions_are_analyzed_independently(self, record: RecordFactory) -> None:
        records = (
            record(session_id="a", relay_seq=1),
            record(session_id="a", relay_seq=3),
            record(session_id="b", relay_seq=1),
            record(session_id="b", relay_seq=2),
        )
        report = GapReport(records)
        assert _session_body(report, "a")["missing_relay_seqs"] == [2]
        assert _session_body(report, "b")["missing_relay_seqs"] == []

    def test_summary_names_each_session_once(self, record: RecordFactory) -> None:
        records = (
            record(session_id="a", relay_seq=1),
            record(session_id="b", relay_seq=1),
        )
        summary = GapReport(records).summary()
        assert summary.count("a:") == 1
        assert summary.count("b:") == 1
