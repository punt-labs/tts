"""Offline sanity tests for the EventTrace JSONL the barge-in evidence rides on.

The live session's machine evidence is this trace: every line must parse,
ordering must survive the round trip, and the serialized shape must keep the
substring contract run_live's summarizer counts on.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from convai import EventTrace
from run_live import _summarize

if TYPE_CHECKING:
    from pathlib import Path


def _lines(path: Path) -> list[dict[str, object]]:
    raw = path.read_text(encoding="utf-8").splitlines()
    return [dict(json.loads(line)) for line in raw]


class TestEventTrace:
    """Serialize/parse round trip and ordering of the JSONL trace."""

    def test_events_round_trip_and_preserve_order(self, tmp_path: Path) -> None:
        path = tmp_path / "trace.jsonl"
        trace = EventTrace(path)
        trace.record("recv", "ping", {"ping_ms": 42})
        trace.record("send", "user_message", {"text": "hello"})
        trace.record("recv", "agent_response", {"text": "hi there"})
        parsed = _lines(path)
        assert [line["type"] for line in parsed] == [
            "ping",
            "user_message",
            "agent_response",
        ]
        assert [line["dir"] for line in parsed] == ["recv", "send", "recv"]
        # Detail payloads merge into the line intact.
        assert parsed[0]["ping_ms"] == 42
        assert parsed[1]["text"] == "hello"

    def test_timestamps_parse_and_ms_is_monotonic(self, tmp_path: Path) -> None:
        path = tmp_path / "trace.jsonl"
        trace = EventTrace(path)
        for n in range(5):
            trace.record("note", "tick", {"n": n})
        parsed = _lines(path)
        stamps = [datetime.fromisoformat(str(line["t"])) for line in parsed]
        assert len(stamps) == 5  # every ISO timestamp parsed
        offsets = [float(str(line["ms"])) for line in parsed]
        assert offsets == sorted(offsets)  # relative clock never runs backward
        assert all(offset >= 0.0 for offset in offsets)

    def test_non_ascii_text_survives_the_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "trace.jsonl"
        text = "réponse ♪ 日本語 — ok"
        EventTrace(path).record("recv", "agent_response", {"text": text})
        assert _lines(path)[0]["text"] == text

    def test_new_trace_truncates_and_creates_parents(self, tmp_path: Path) -> None:
        path = tmp_path / "results" / "trace.jsonl"
        path.parent.mkdir()
        path.write_text("stale line from a previous run\n", encoding="utf-8")
        trace = EventTrace(path)
        assert path.read_text(encoding="utf-8") == ""  # old run wiped
        trace.record("note", "run_config", {"seed_bytes": 1024})
        assert len(_lines(path)) == 1
        nested = tmp_path / "deep" / "nested" / "trace.jsonl"
        EventTrace(nested).record("note", "x", {})
        assert nested.exists()  # parents created on demand

    def test_detail_keys_cannot_overwrite_trace_stamps(self, tmp_path: Path) -> None:
        # Handlers record server event bodies verbatim; a body carrying
        # t/ms/dir/type must not clobber the trace's own stamps, or the
        # evidence line lies about what happened and when.
        path = tmp_path / "trace.jsonl"
        trace = EventTrace(path)
        trace.record(
            "recv",
            "interruption",
            {"type": "spoofed", "dir": "send", "ms": -1.0, "t": "junk", "event_id": 7},
        )
        line = _lines(path)[0]
        assert line["type"] == "interruption"
        assert line["dir"] == "recv"
        assert float(str(line["ms"])) >= 0.0
        # The timestamp stamp survived (would raise on the "junk" spoof).
        assert datetime.fromisoformat(str(line["t"])).tzinfo is not None
        assert line["event_id"] == 7  # genuine detail still merges

    def test_summarize_counts_the_barge_in_evidence(self, tmp_path: Path) -> None:
        # run_live._summarize counts trace lines by the '"type": "<name>"'
        # substring; this pins the serializer to that contract.
        path = tmp_path / "trace.jsonl"
        trace = EventTrace(path)
        trace.record("recv", "client_tool_call", {"tool": "clock"})
        trace.record("recv", "client_tool_call", {"tool": "search_code"})
        trace.record("recv", "interruption", {})
        trace.record("recv", "agent_response_correction", {})
        trace.record("recv", "audio", {"bytes_b64": 128})
        summary = _summarize(path)
        assert summary == "tool calls: 2, interruptions: 1, corrections: 1"

    def test_summarize_ignores_type_substrings_in_text(self, tmp_path: Path) -> None:
        # Only the line's own type field counts; an agent/user text payload
        # that happens to quote an event shape must not inflate the tallies.
        # (JSON escapes the quotes inside a string, so this case is safe by
        # serialization; the test keeps it that way.)
        path = tmp_path / "trace.jsonl"
        trace = EventTrace(path)
        quoted = (
            'the log shows "type": "interruption" and '
            '"type": "client_tool_call" in a reply'
        )
        trace.record("recv", "agent_response", {"text": quoted})
        trace.record("recv", "interruption", {})
        assert _summarize(path) == "tool calls: 0, interruptions: 1, corrections: 0"

    def test_summarize_ignores_types_nested_in_details(self, tmp_path: Path) -> None:
        # Event bodies recorded verbatim can nest mappings; a nested
        # {"type": "client_tool_call"} serializes UNescaped, so a substring
        # counter would tally it. Only the line's own type may count.
        path = tmp_path / "trace.jsonl"
        trace = EventTrace(path)
        nested: dict[str, object] = {"reason": {"type": "client_tool_call"}}
        trace.record("recv", "interruption", nested)
        assert _summarize(path) == "tool calls: 0, interruptions: 1, corrections: 0"
