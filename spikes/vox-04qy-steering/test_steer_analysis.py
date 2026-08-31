"""Pins for the transcript analysis the Arm 1 verdict numbers come from.

Built over hand-stamped transcripts so every latency is exact arithmetic,
not wall clock.
"""

from __future__ import annotations

import json

import pytest

from rpc_protocol import Transcript
from steer_analysis import TranscriptAnalysis

_MS = 1_000_000  # nanoseconds per millisecond


def _line(event_type: str, **fields: object) -> str:
    return json.dumps({"type": event_type, **fields})


def _steered_transcript() -> Transcript:
    transcript = Transcript()
    transcript.note_send(_line("prompt", message="work"), ns=0)
    transcript.note_recv(_line("response", command="prompt", success=True), ns=1 * _MS)
    transcript.note_recv(_line("agent_start"), ns=2 * _MS)
    transcript.note_recv(
        _line("tool_execution_start", toolCallId="t1", toolName="read"), ns=10 * _MS
    )
    transcript.note_send(_line("steer", message="STOP"), ns=20 * _MS)
    transcript.note_recv(_line("response", command="steer", success=True), ns=23 * _MS)
    transcript.note_recv(
        _line("queue_update", steering=["STOP"], followUp=[]), ns=24 * _MS
    )
    transcript.note_recv(
        _line("tool_execution_end", toolCallId="t1", toolName="read"), ns=30 * _MS
    )
    transcript.note_recv(
        _line("message_end", message={"content": [{"text": "STEERED-ACK"}]}),
        ns=60 * _MS,
    )
    transcript.note_recv(_line("agent_end"), ns=70 * _MS)
    return transcript


class TestSendNs:
    """Locating the command whose latency is being measured."""

    def test_finds_the_steer_send_stamp(self) -> None:
        analysis = TranscriptAnalysis(_steered_transcript())
        assert analysis.send_ns("steer") == 20 * _MS

    def test_absent_command_raises(self) -> None:
        analysis = TranscriptAnalysis(_steered_transcript())
        with pytest.raises(LookupError, match="abort"):
            analysis.send_ns("abort")


class TestFirstEventAfter:
    """The 'first steered output' side of every latency pair."""

    def test_finds_first_matching_event_after_the_stamp(self) -> None:
        analysis = TranscriptAnalysis(_steered_transcript())
        event = analysis.first_event_after(
            20 * _MS, lambda e: e.contains("STEERED-ACK"), description="marker"
        )
        assert event.recv_ns == 60 * _MS

    def test_events_before_the_stamp_are_ignored(self) -> None:
        analysis = TranscriptAnalysis(_steered_transcript())
        event = analysis.first_event_after(
            20 * _MS,
            lambda e: e.type == "tool_execution_end",
            description="tool end",
        )
        assert event.recv_ns == 30 * _MS

    def test_no_match_raises_with_the_description(self) -> None:
        analysis = TranscriptAnalysis(_steered_transcript())
        with pytest.raises(LookupError, match="unicorn"):
            analysis.first_event_after(
                0, lambda e: e.contains("nope"), description="unicorn"
            )


class TestLatencyAndTimeline:
    """The numbers and the shape the summary file carries."""

    def test_elapsed_ms_is_exact(self) -> None:
        analysis = TranscriptAnalysis(_steered_transcript())
        marker = analysis.first_event_after(
            20 * _MS, lambda e: e.contains("STEERED-ACK"), description="marker"
        )
        assert analysis.elapsed_ms(analysis.send_ns("steer"), marker.recv_ns) == 40.0

    def test_timeline_offsets_from_first_entry(self) -> None:
        analysis = TranscriptAnalysis(_steered_transcript())
        timeline = analysis.timeline()
        assert timeline[0] == {"ms": 0.0, "dir": "send", "label": "prompt"}
        assert timeline[4] == {"ms": 20.0, "dir": "send", "label": "steer"}
        assert timeline[-1]["label"] == "agent_end"

    def test_in_flight_tool_end_is_observable_after_steer(self) -> None:
        # The bst7 barge-in question on the pi side: the tool that was
        # running when steer landed still completes — its end event is
        # after the steer stamp.
        analysis = TranscriptAnalysis(_steered_transcript())
        start = analysis.first_event_after(
            0, lambda e: e.type == "tool_execution_start", description="tool start"
        )
        end = analysis.first_event_after(
            analysis.send_ns("steer"),
            lambda e: (
                e.type == "tool_execution_end"
                and e.data.get("toolCallId") == start.data.get("toolCallId")
            ),
            description="in-flight tool end",
        )
        assert end.recv_ns > analysis.send_ns("steer")
