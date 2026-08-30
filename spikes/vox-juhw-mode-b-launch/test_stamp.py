"""Ordering, attribution, redaction, and durability of the hook ledger.

These are the verdict-bearing properties for evidence item 1 (hooks land in
the stub store, ordered, with the session identifiable) -- proven here
without spawning any real claude session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from stamp import UNATTRIBUTED, HookLedger, HookRecord, SequenceStamper

if TYPE_CHECKING:
    from pathlib import Path


class TestSequenceStamper:
    """Global and per-session monotonic stamps with session attribution."""

    def test_recv_seq_is_globally_monotonic_across_sessions(self) -> None:
        stamper = SequenceStamper()
        records = [
            stamper.stamp("SessionStart", {"session_id": "a"}),
            stamper.stamp("SessionStart", {"session_id": "b"}),
            stamper.stamp("Stop", {"session_id": "a"}),
            stamper.stamp("Stop", {"session_id": "b"}),
        ]
        assert [r.recv_seq for r in records] == [1, 2, 3, 4]

    def test_session_seq_counts_per_session_from_one(self) -> None:
        stamper = SequenceStamper()
        a1 = stamper.stamp("SessionStart", {"session_id": "a"})
        b1 = stamper.stamp("SessionStart", {"session_id": "b"})
        a2 = stamper.stamp("PostToolUse", {"session_id": "a"})
        a3 = stamper.stamp("Stop", {"session_id": "a"})
        b2 = stamper.stamp("Stop", {"session_id": "b"})
        assert [a1.session_seq, a2.session_seq, a3.session_seq] == [1, 2, 3]
        assert [b1.session_seq, b2.session_seq] == [1, 2]

    def test_session_id_is_attributed_from_payload(self) -> None:
        stamper = SequenceStamper()
        record = stamper.stamp("Stop", {"session_id": "sess-42", "cwd": "/x"})
        assert record.session_id == "sess-42"
        assert record.event == "Stop"

    @pytest.mark.parametrize("payload", [{}, {"session_id": ""}, {"session_id": 7}])
    def test_missing_or_bad_session_id_falls_back(
        self, payload: dict[str, object]
    ) -> None:
        record = SequenceStamper().stamp("Stop", payload)
        assert record.session_id == UNATTRIBUTED

    def test_credential_shaped_fields_are_redacted(self) -> None:
        stamper = SequenceStamper()
        record = stamper.stamp(
            "SessionStart",
            {
                "session_id": "a",
                "persistent_session_token": "tok-123",
                "signed_url": "wss://x?auth=y",
                "MY_API_KEY": "k",
                "cwd": "/proj",
            },
        )
        assert record.payload["persistent_session_token"] == "[redacted]"
        assert record.payload["signed_url"] == "[redacted]"
        assert record.payload["MY_API_KEY"] == "[redacted]"
        assert record.payload["cwd"] == "/proj"

    def test_redaction_is_recursive_into_nested_dicts(self) -> None:
        # The ledger is a committed run artifact; tool_input/tool_response
        # nest arbitrary structures, so masking must reach every depth.
        record = SequenceStamper().stamp(
            "PostToolUse",
            {
                "session_id": "a",
                "tool_input": {
                    "file_path": "/proj/x.py",
                    "auth": {"api_key": "k-123", "region": "us"},
                },
            },
        )
        tool_input = record.payload["tool_input"]
        assert isinstance(tool_input, dict)
        auth = tool_input["auth"]
        assert isinstance(auth, dict)
        assert auth["api_key"] == "[redacted]"
        assert auth["region"] == "us"
        assert tool_input["file_path"] == "/proj/x.py"

    def test_redaction_reaches_dicts_inside_lists(self) -> None:
        record = SequenceStamper().stamp(
            "PostToolUse",
            {
                "session_id": "a",
                "tool_response": {
                    "results": [
                        {"name": "ok", "value": 1},
                        {"secret": "s3cr3t", "value": 2},
                    ]
                },
            },
        )
        response = record.payload["tool_response"]
        assert isinstance(response, dict)
        results = response["results"]
        assert isinstance(results, list)
        assert results[0] == {"name": "ok", "value": 1}
        assert results[1] == {"secret": "[redacted]", "value": 2}

    def test_none_values_survive_redaction_as_none(self) -> None:
        record = SequenceStamper().stamp(
            "PostToolUse",
            {"session_id": "a", "tool_response": None, "extras": {"note": None}},
        )
        assert record.payload["tool_response"] is None
        extras = record.payload["extras"]
        assert isinstance(extras, dict)
        assert extras["note"] is None


class TestHookRecordRoundTrip:
    """JSONL serialization survives a parse round trip; bad lines raise."""

    def test_round_trip_preserves_all_fields(self) -> None:
        original = SequenceStamper().stamp("Stop", {"session_id": "s", "n": 1})
        parsed = HookRecord.from_json(original.to_json())
        assert parsed == original

    def test_non_object_line_raises(self) -> None:
        with pytest.raises(ValueError, match="not an object"):
            HookRecord.from_json("[1, 2]")

    def test_missing_field_raises(self) -> None:
        with pytest.raises(ValueError, match="missing field"):
            HookRecord.from_json('{"recv_seq": 1}')

    def test_non_object_payload_raises(self) -> None:
        line = (
            '{"recv_seq": 1, "session_seq": 1, "session_id": "s", '
            '"event": "Stop", "received_at": "t", "payload": 3}'
        )
        with pytest.raises(ValueError, match="payload is not an object"):
            HookRecord.from_json(line)


class TestHookLedger:
    """Append-only persistence that survives the store being killed."""

    def test_appended_records_read_back_in_order(self, tmp_path: Path) -> None:
        ledger = HookLedger(tmp_path / "ledger.jsonl")
        stamper = SequenceStamper()
        for event in ("SessionStart", "UserPromptSubmit", "Stop"):
            ledger.append(stamper.stamp(event, {"session_id": "s"}))
        events = [r.event for r in ledger.records()]
        assert events == ["SessionStart", "UserPromptSubmit", "Stop"]
        assert [r.recv_seq for r in ledger.records()] == [1, 2, 3]

    def test_missing_file_reads_as_empty(self, tmp_path: Path) -> None:
        assert HookLedger(tmp_path / "absent.jsonl").records() == ()

    def test_append_creates_parent_directories(self, tmp_path: Path) -> None:
        ledger = HookLedger(tmp_path / "deep" / "run" / "ledger.jsonl")
        ledger.append(SequenceStamper().stamp("Stop", {"session_id": "s"}))
        assert len(ledger.records()) == 1
