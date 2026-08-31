"""Pins for the capture run's timepoint sampling.

The four timepoints are evidence only if they are four DISTINCT moments.
A seeded prompt whose first action is the failing suite must not let
``early`` and ``mid-debug`` collapse onto one cutoff -- mid-debug requires
a failing record strictly AFTER early's sampled cutoff.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast, final

from conftest import RecordFactory
from run_capture import TimepointSampler

if TYPE_CHECKING:
    from pathlib import Path

    from launcher import TmuxSession
    from stamp import HookRecord


@final
class _DeadSession:
    """A never-alive session stand-in; capture must not be asked for."""

    __slots__ = ()

    def alive(self) -> bool:
        return False

    def capture(self) -> str:
        msg = "capture() must not be called on a dead session"
        raise AssertionError(msg)


def _sampler(tmp_path: Path) -> TimepointSampler:
    return TimepointSampler(cast("TmuxSession", _DeadSession()), tmp_path)


def _failing(record: RecordFactory) -> HookRecord:
    return record(payload={"tool_response": "FAILED (errors=1)\nexit code 1"})


class TestTimepointSampler:
    """early and mid-debug can never alias onto the same cutoff."""

    def test_first_failing_post_tool_use_samples_only_early(
        self, record: RecordFactory, tmp_path: Path
    ) -> None:
        # The aliasing scenario: the fork's FIRST action is the failing
        # suite. Both triggers match the record, but only early may fire.
        sampler = _sampler(tmp_path)
        failing = _failing(record)
        assert sampler.observe((failing,)) == ("early",)
        # Re-observing the same snapshot must not promote the same
        # record into mid-debug -- there is nothing after the cutoff.
        assert sampler.observe((failing,)) == ()

    def test_mid_debug_fires_on_a_strictly_later_failure(
        self, record: RecordFactory, tmp_path: Path
    ) -> None:
        sampler = _sampler(tmp_path)
        first_failure = _failing(record)
        assert sampler.observe((first_failure,)) == ("early",)
        later_failure = _failing(record)
        assert sampler.observe((first_failure, later_failure)) == ("mid-debug",)
        assert sampler.samples["early"]["cutoff_index"] == 1
        assert sampler.samples["mid-debug"]["cutoff_index"] == 2

    def test_mid_debug_never_fires_before_early_is_sampled(
        self, record: RecordFactory, tmp_path: Path
    ) -> None:
        # Within ONE observe pass over a failing-first ledger, early
        # samples at the snapshot length, so mid-debug is structurally
        # deferred to a later snapshot even when both triggers match.
        sampler = _sampler(tmp_path)
        records = (_failing(record), _failing(record))
        assert sampler.observe(records) == ("early",)
        assert sampler.samples["early"]["cutoff_index"] == 2
        assert "mid-debug" not in sampler.samples
