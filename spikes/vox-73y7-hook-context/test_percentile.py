"""Pins for the nearest-rank percentile summary the analyzers share.

Latency verdicts quote p50/p95; if the rank math is off by one the report
quotes the wrong sample. Known-answer fixtures hold it exact.
"""

from __future__ import annotations

from percentile import PercentileStats


class TestNearestRank:
    """Known-answer percentiles on constructed samples."""

    def test_one_to_hundred(self) -> None:
        stats = PercentileStats.of([float(v) for v in range(1, 101)])
        # Nearest-rank on 1..100: p50 = ceil(0.50*100) = 50th value,
        # p95 = ceil(0.95*100) = 95th value.
        assert (stats.n, stats.p50, stats.p95, stats.max) == (100, 50.0, 95.0, 100.0)

    def test_single_sample_is_its_own_every_percentile(self) -> None:
        stats = PercentileStats.of([7.5])
        assert (stats.n, stats.p50, stats.p95, stats.max) == (1, 7.5, 7.5, 7.5)

    def test_two_samples_p50_is_the_lower(self) -> None:
        # ceil(0.5 * 2) - 1 = index 0: nearest-rank takes the lower value.
        stats = PercentileStats.of([10.0, 20.0])
        assert (stats.p50, stats.p95, stats.max) == (10.0, 20.0, 20.0)

    def test_empty_sample_is_all_zeros(self) -> None:
        stats = PercentileStats.of([])
        assert (stats.n, stats.p50, stats.p95, stats.max) == (0, 0.0, 0.0, 0.0)

    def test_input_order_does_not_matter(self) -> None:
        shuffled = [30.0, 10.0, 50.0, 20.0, 40.0]
        stats = PercentileStats.of(shuffled)
        assert (stats.p50, stats.p95, stats.max) == (30.0, 50.0, 50.0)


class TestRendering:
    """The dict and table forms quote the same numbers."""

    def test_as_dict_rounds_to_one_decimal(self) -> None:
        stats = PercentileStats.of([1.234, 5.678, 9.999])
        assert stats.as_dict() == {"n": 3, "p50": 5.7, "p95": 10.0, "max": 10.0}

    def test_row_aligns_under_header(self) -> None:
        header = PercentileStats.header()
        row = PercentileStats.of([1.0]).row("hook_fire_to_receipt_ms")
        assert len(header) == len(row)
