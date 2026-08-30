"""Pins for the per-hook-type field inventory (bead question a).

The verdict quotes this analyzer's numbers: which fields each event type
carries, how they classify, and how much of each payload is state. A wrong
class or a miscounted presence fraction answers the bead question wrong,
so the classification and the arithmetic are held on constructed ledgers
mixing state-bearing and metadata-only events.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from field_inventory import FieldInventory

if TYPE_CHECKING:
    from conftest import RecordFactory


def _profile(inventory: FieldInventory, event: str) -> dict[str, object]:
    return cast("dict[str, object]", inventory.as_dict()[event])


def _fields(profile: dict[str, object]) -> dict[str, dict[str, object]]:
    return cast("dict[str, dict[str, object]]", profile["fields"])


class TestFieldClassification:
    """State, pointer, and metadata land in the right buckets."""

    def test_mixed_fixture_classifies_per_hook_type(
        self, record: RecordFactory
    ) -> None:
        records = (
            record(
                event="UserPromptSubmit",
                payload={
                    "prompt": "fix the failing test",
                    "cwd": "/scratch/project",
                    "transcript_path": "/scratch/t.jsonl",
                    "permission_mode": "acceptEdits",
                },
            ),
            record(
                event="PostToolUse",
                payload={
                    "tool_name": "Write",
                    "tool_input": {"file_path": "/scratch/project/a.py"},
                    "tool_response": {"type": "create"},
                    "relay_seq": 2,
                },
            ),
            record(event="SessionEnd", payload={"reason": "clear"}),
        )
        inventory = FieldInventory(records)

        prompt_fields = _fields(_profile(inventory, "UserPromptSubmit"))
        assert prompt_fields["prompt"]["class"] == "state"
        assert prompt_fields["cwd"]["class"] == "pointer"
        assert prompt_fields["transcript_path"]["class"] == "pointer"
        assert prompt_fields["permission_mode"]["class"] == "metadata"

        tool_fields = _fields(_profile(inventory, "PostToolUse"))
        assert tool_fields["tool_input"]["class"] == "state"
        assert tool_fields["tool_response"]["class"] == "state"
        assert tool_fields["tool_name"]["class"] == "state"
        assert tool_fields["relay_seq"]["class"] == "metadata"

        end_fields = _fields(_profile(inventory, "SessionEnd"))
        assert end_fields["reason"]["class"] == "metadata"
        assert end_fields["session_id"]["class"] == "metadata"

    def test_metadata_only_event_has_zero_state_bytes(
        self, record: RecordFactory
    ) -> None:
        inventory = FieldInventory((record(event="SessionEnd"),))
        profile = _profile(inventory, "SessionEnd")
        state = cast("dict[str, float]", profile["state_bytes"])
        assert state["max"] == 0.0

    def test_state_bytes_are_the_exact_state_field_share(
        self, record: RecordFactory
    ) -> None:
        prompt_text = "run the suite and fix the bug"
        rec = record(event="UserPromptSubmit", payload={"prompt": prompt_text})
        inventory = FieldInventory((rec,))
        profile = _profile(inventory, "UserPromptSubmit")
        state = cast("dict[str, float]", profile["state_bytes"])
        payload = cast("dict[str, float]", profile["payload_bytes"])
        expected_state = float(len(json.dumps(prompt_text).encode("utf-8")))
        assert state["max"] == expected_state
        assert payload["max"] == float(len(json.dumps(rec.payload).encode("utf-8")))
        assert state["max"] < payload["max"]  # metadata is never counted as state


class TestPresenceFractions:
    """A field seen in some records of a type reports its exact fraction."""

    def test_half_present_field_reports_half(self, record: RecordFactory) -> None:
        records = (
            record(event="Notification", payload={"message": "permission needed"}),
            record(event="Notification"),
        )
        fields = _fields(_profile(FieldInventory(records), "Notification"))
        assert fields["message"]["presence"] == 0.5
        assert fields["session_id"]["presence"] == 1.0

    def test_counts_are_per_event_type(self, record: RecordFactory) -> None:
        records = (
            record(event="PostToolUse"),
            record(event="PostToolUse"),
            record(event="Stop"),
        )
        inventory = FieldInventory(records)
        assert _profile(inventory, "PostToolUse")["count"] == 2
        assert _profile(inventory, "Stop")["count"] == 1


class TestEdges:
    """Empty ledger and table rendering."""

    def test_empty_ledger_is_an_empty_inventory(self) -> None:
        inventory = FieldInventory(())
        assert inventory.as_dict() == {}

    def test_table_has_two_rows_per_event_type(self, record: RecordFactory) -> None:
        inventory = FieldInventory((record(event="Stop"), record(event="Stop")))
        lines = inventory.table().splitlines()
        # header + rule + payload row + state row
        assert len(lines) == 4
        assert "Stop payload_bytes" in lines[2]
        assert "Stop state_bytes" in lines[3]
