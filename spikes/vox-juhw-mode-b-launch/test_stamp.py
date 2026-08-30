"""Ordering, attribution, redaction, and durability of the hook ledger.

These are the verdict-bearing properties for evidence item 1 (hooks land in
the stub store, ordered, with the session identifiable) -- proven here
without spawning any real claude session.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stamp import UNATTRIBUTED, HookLedger, HookRecord, Sanitizer, SequenceStamper


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


class TestSanitizer:
    """Host paths in persisted payloads become stable placeholders."""

    def test_scratch_rule_applies_before_home_rule(self, tmp_path: Path) -> None:
        # The scratch root lives under home; ordering makes it win.
        sanitizer = Sanitizer.for_host(tmp_path / "scratch")
        scrubbed = sanitizer.scrub(f"{tmp_path}/scratch/proj/greeting.py")
        assert scrubbed == "<scratch>/proj/greeting.py"

    def test_home_prefix_becomes_tilde(self) -> None:
        sanitizer = Sanitizer.for_host()
        assert sanitizer.scrub(f"{Path.home()}/notes.txt") == "~/notes.txt"

    def test_non_path_text_is_untouched(self) -> None:
        sanitizer = Sanitizer.for_host()
        assert sanitizer.scrub("plain words, no paths") == "plain words, no paths"

    def test_dash_encoded_prefixes_are_scrubbed_too(self, tmp_path: Path) -> None:
        # Claude Code's projects/ dir encodes the project path with "/"
        # and "." turned into "-"; transcript paths carry that slug and
        # would re-leak the username past the plain prefix rules.
        scratch = tmp_path / "scratch"
        sanitizer = Sanitizer.for_host(scratch)
        encoded = str(scratch).replace("/", "-").replace(".", "-")
        scrubbed = sanitizer.scrub(f"projects/{encoded}-project/x.jsonl")
        assert scrubbed == "projects/<scratch-slug>-project/x.jsonl"
        home_encoded = str(Path.home()).replace("/", "-").replace(".", "-")
        assert sanitizer.scrub(home_encoded) == "<home-slug>"

    def test_stamper_sanitizes_string_values_at_any_depth(self, tmp_path: Path) -> None:
        scratch = tmp_path / "scratch"
        stamper = SequenceStamper(Sanitizer.for_host(scratch))
        record = stamper.stamp(
            "PostToolUse",
            {
                "session_id": "s",
                "cwd": f"{scratch}/proj",
                "tool_input": {"file_path": f"{scratch}/proj/greeting.py"},
                "count": 3,
                "flag": None,
            },
        )
        assert record.payload["cwd"] == "<scratch>/proj"
        tool_input = record.payload["tool_input"]
        assert isinstance(tool_input, dict)
        assert tool_input["file_path"] == "<scratch>/proj/greeting.py"
        assert record.payload["count"] == 3
        assert record.payload["flag"] is None


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

    def test_snapshot_skips_only_an_unterminated_final_line(
        self, tmp_path: Path
    ) -> None:
        # A concurrent poll can catch the store mid-append: the file ends
        # in a fragment without a trailing newline. The snapshot read
        # returns the complete records; the strict read fails loud.
        ledger = HookLedger(tmp_path / "ledger.jsonl")
        stamper = SequenceStamper()
        for event in ("SessionStart", "UserPromptSubmit"):
            ledger.append(stamper.stamp(event, {"session_id": "s"}))
        with ledger.path.open("a", encoding="utf-8") as handle:
            handle.write('{"recv_seq": 3, "session_')  # torn, no newline
        snapshot = ledger.records_snapshot()
        assert [r.event for r in snapshot] == ["SessionStart", "UserPromptSubmit"]
        with pytest.raises(ValueError):
            ledger.records()

    def test_snapshot_with_terminated_final_line_returns_everything(
        self, tmp_path: Path
    ) -> None:
        ledger = HookLedger(tmp_path / "ledger.jsonl")
        stamper = SequenceStamper()
        for event in ("SessionStart", "Stop"):
            ledger.append(stamper.stamp(event, {"session_id": "s"}))
        assert ledger.records_snapshot() == ledger.records()

    def test_snapshot_still_fails_loud_on_a_torn_middle_line(
        self, tmp_path: Path
    ) -> None:
        # Only the unterminated tail is a write in progress; a malformed
        # line WITH a newline is corruption in both read modes.
        path = tmp_path / "ledger.jsonl"
        good = SequenceStamper().stamp("SessionStart", {"session_id": "s"})
        path.write_text('{"torn": \n' + good.to_json() + "\n", encoding="utf-8")
        ledger = HookLedger(path)
        with pytest.raises(ValueError):
            ledger.records_snapshot()

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
