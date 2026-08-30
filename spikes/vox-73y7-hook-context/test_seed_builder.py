"""Pins for the bounded seed builder (bead d).

The seed's whole claim is "hand-picked context under ~10KB": these pins
hold the budget on oversized ledgers, the oldest-first trim order, the
category picks (goal, active files, freshest failure), the empty-ledger
boundary, and -- end-to-end through the real stamper -- that credential
values can never ride a seed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from seed_builder import (
    SEED_BUDGET_BYTES,
    SeedBuilder,
    SeedReconstructor,
)
from stamp import SequenceStamper

if TYPE_CHECKING:
    from conftest import RecordFactory
    from stamp import HookRecord

# 2000 quote characters: each escapes to two bytes in JSON, so one such
# tool response costs ~4KB serialized -- three of them overflow the budget
# and force exactly one trim.
_FAT_RESPONSE = '"' * 2000


def _fat_tool_use(record: RecordFactory, path: str) -> HookRecord:
    return record(
        event="PostToolUse",
        payload={
            "tool_name": "Bash",
            "tool_input": {"file_path": path},
            "tool_response": _FAT_RESPONSE,
        },
    )


class TestBudget:
    """The serialized seed never exceeds ~10KB."""

    def test_oversized_ledger_is_trimmed_under_the_budget(
        self, record: RecordFactory
    ) -> None:
        records = tuple(_fat_tool_use(record, f"/p/f{i}.py") for i in range(3))
        seed = SeedBuilder(records, cutoff_recv_seq=3).build()
        assert seed.byte_size() <= SEED_BUDGET_BYTES

    def test_trim_drops_the_oldest_result_first(self, record: RecordFactory) -> None:
        # Three fat results overflow; the survivor set must be the NEWEST
        # ones -- a seed that forgets the latest result answers the wrong
        # timepoint.
        records = tuple(
            record(
                event="PostToolUse",
                payload={"tool_name": tool, "tool_response": _FAT_RESPONSE},
            )
            for tool in ("Read", "Edit", "Bash")  # receipt order: Read oldest
        )
        seed = SeedBuilder(records, cutoff_recv_seq=3).build()
        assert len(seed.last_tool_results) < 3
        assert seed.last_tool_results[-1].startswith("Bash:")
        assert all(not r.startswith("Read:") for r in seed.last_tool_results)

    def test_single_oversized_result_still_fits(self, record: RecordFactory) -> None:
        # One result is capped at 2000 chars before serialization, so a
        # single-item seed always fits without trimming to empty.
        seed = SeedBuilder(
            (_fat_tool_use(record, "/p/only.py"),), cutoff_recv_seq=1
        ).build()
        assert seed.byte_size() <= SEED_BUDGET_BYTES
        assert len(seed.last_tool_results) == 1


class TestCategoryPicks:
    """Goal, active files, and the freshest failure are the seed."""

    def test_picks_goal_files_and_open_failure(self, record: RecordFactory) -> None:
        records = (
            record(
                event="UserPromptSubmit",
                payload={"prompt": "Fix the failing suite."},
            ),
            record(
                event="PostToolUse",
                payload={
                    "tool_name": "Edit",
                    "tool_input": {"file_path": "/proj/stats.py"},
                    "tool_response": "edited",
                },
            ),
            record(
                event="PostToolUse",
                payload={
                    "tool_name": "Bash",
                    "tool_input": {"command": "python3 -m unittest"},
                    "tool_response": "AssertionError: 1 != 4\nFAILED (failures=1)",
                },
            ),
        )
        seed = SeedBuilder(records, cutoff_recv_seq=3).build()
        assert seed.current_goal == "Fix the failing suite."
        assert seed.active_files == ("/proj/stats.py",)
        assert "AssertionError" in seed.recent_failure
        assert seed.last_tool_results[-1].startswith("Bash:")

    def test_seed_reconstructor_answers_in_the_shared_shape(
        self, record: RecordFactory
    ) -> None:
        records = (
            record(event="UserPromptSubmit", payload={"prompt": "the goal"}),
            record(
                event="PostToolUse",
                payload={"tool_name": "Bash", "tool_response": "all good"},
            ),
        )
        seed = SeedBuilder(records, cutoff_recv_seq=2).build()
        answer = SeedReconstructor(seed).answer("t1")
        assert answer.source == "seed-only"
        assert answer.goal == "the goal"
        assert answer.last_result.endswith("all good")
        assert "What was I just doing?" in answer.render()

    def test_cutoff_bounds_what_the_seed_may_see(self, record: RecordFactory) -> None:
        records = (
            record(event="UserPromptSubmit", payload={"prompt": "first goal"}),
            record(event="UserPromptSubmit", payload={"prompt": "later goal"}),
        )
        seed = SeedBuilder(records, cutoff_recv_seq=1).build()
        assert seed.current_goal == "first goal"


class TestEmptyLedger:
    """Nothing visible yields an empty seed, not a crash."""

    def test_empty_ledger_builds_an_empty_seed(self) -> None:
        seed = SeedBuilder((), cutoff_recv_seq=99).build()
        assert seed.current_goal == ""
        assert seed.active_files == ()
        assert seed.last_tool_results == ()
        assert seed.byte_size() <= SEED_BUDGET_BYTES
        assert SeedReconstructor(seed).answer("t0").render()


class TestRedactionEndToEnd:
    """Through the real stamper: credential values never ride a seed."""

    def test_stamped_credentials_cannot_reach_the_seed(self) -> None:
        stamper = SequenceStamper()
        records = (
            stamper.stamp(
                "UserPromptSubmit",
                {"session_id": "s", "prompt": "deploy the service"},
            ),
            stamper.stamp(
                "PostToolUse",
                {
                    "session_id": "s",
                    "tool_name": "Bash",
                    "tool_input": {
                        "command": "curl -H @headers",
                        "api_key": "sk-live-VERYSECRET",
                    },
                    "tool_response": {"signed_url": "wss://x?sig=SIGSECRET"},
                },
            ),
        )
        seed = SeedBuilder(records, cutoff_recv_seq=2).build()
        body = seed.to_json()
        assert "sk-live-VERYSECRET" not in body
        assert "SIGSECRET" not in body
        assert "[redacted]" in body
