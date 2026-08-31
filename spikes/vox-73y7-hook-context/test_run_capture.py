"""Pins for the capture run's timepoint sampling.

The four timepoints are evidence only if they are four DISTINCT moments,
so each label requires its trigger record strictly AFTER its
predecessor's sampled cutoff: a seeded prompt whose first action is the
failing suite must not collapse ``early`` and ``mid-debug`` onto one
cutoff, and a turn-boundary ``Stop`` (Claude Code fires one at EVERY
turn end) must not alias ``end`` onto ``early``.
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


def _passing(record: RecordFactory) -> HookRecord:
    return record(payload={"tool_response": "Ran 5 tests in 0.01s\nOK"})


def _stop(record: RecordFactory) -> HookRecord:
    return record(event="Stop")


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

    def test_stop_in_the_first_snapshot_samples_only_early(
        self, record: RecordFactory, tmp_path: Path
    ) -> None:
        # Claude Code fires Stop at every turn boundary: a first snapshot
        # holding both a PostToolUse and the first turn's Stop must not
        # give early and end the same cutoff.
        sampler = _sampler(tmp_path)
        snapshot = (_passing(record), _stop(record))
        assert sampler.observe(snapshot) == ("early",)
        assert sampler.observe(snapshot) == ()
        assert "end" not in sampler.samples

    def test_end_requires_a_stop_strictly_after_post_fix(
        self, record: RecordFactory, tmp_path: Path
    ) -> None:
        sampler = _sampler(tmp_path)
        first_failure = _failing(record)
        assert sampler.observe((first_failure,)) == ("early",)
        second_failure = _failing(record)
        turn_stop = _stop(record)
        assert sampler.observe((first_failure, second_failure, turn_stop)) == (
            "mid-debug",
        )
        # The fix lands; the earlier turn-boundary Stop (index 3) is at
        # or before post-fix's cutoff and must not close the run.
        fixed = _passing(record)
        assert sampler.observe((first_failure, second_failure, turn_stop, fixed)) == (
            "post-fix",
        )
        assert "end" not in sampler.samples
        final_stop = _stop(record)
        assert sampler.observe(
            (first_failure, second_failure, turn_stop, fixed, final_stop)
        ) == ("end",)
        assert sampler.samples["post-fix"]["cutoff_index"] == 4
        assert sampler.samples["end"]["cutoff_index"] == 5
        assert sampler.done()
