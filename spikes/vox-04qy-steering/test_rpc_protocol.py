"""Pins for the pi RPC wire objects: commands out, events in, transcript.

The live probe confirmed the wire shapes (`{"type":"steer","message":...}`
in; `{"type":"response","command":"steer","success":true}` and
`{"type":"queue_update","steering":[...],"followUp":[...]}` out). These
tests pin the encoding, the event parse, and the stamped transcript the
evidence files are written from — offline, no pi process.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rpc_protocol import RpcCommand, RpcEvent, Transcript
from stamp import Sanitizer


class TestRpcCommand:
    """Command encoding: one JSON line per command, exact field shapes."""

    def test_prompt_encodes_type_and_message(self) -> None:
        wire = json.loads(RpcCommand.prompt("hello").to_wire())
        assert wire == {"type": "prompt", "message": "hello"}

    def test_steer_encodes_type_and_message(self) -> None:
        wire = json.loads(RpcCommand.steer("change course").to_wire())
        assert wire == {"type": "steer", "message": "change course"}

    def test_follow_up_encodes_type_and_message(self) -> None:
        wire = json.loads(RpcCommand.follow_up("and then").to_wire())
        assert wire == {"type": "follow_up", "message": "and then"}

    def test_abort_carries_no_message_field(self) -> None:
        wire = json.loads(RpcCommand.abort().to_wire())
        assert wire == {"type": "abort"}

    def test_wire_form_is_a_single_line(self) -> None:
        assert "\n" not in RpcCommand.prompt("a\nmultiline\nprompt").to_wire()

    @pytest.mark.parametrize("factory", ["prompt", "steer", "follow_up"])
    def test_empty_message_is_refused(self, factory: str) -> None:
        build = getattr(RpcCommand, factory)
        with pytest.raises(ValueError, match="empty"):
            build("")

    def test_command_type_is_exposed(self) -> None:
        assert RpcCommand.steer("x").command_type == "steer"


class TestRpcEvent:
    """Event parse: every stdout line becomes a typed, stamped event."""

    def test_parses_type_and_keeps_raw_data(self) -> None:
        event = RpcEvent('{"type":"agent_end","messages":[]}', recv_ns=7)
        assert event.type == "agent_end"
        assert event.recv_ns == 7
        assert event.data["messages"] == []

    def test_non_object_line_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not an object"):
            RpcEvent("[1,2]", recv_ns=1)

    def test_missing_type_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no type"):
            RpcEvent('{"data": 1}', recv_ns=1)

    def test_response_matching_by_command(self) -> None:
        line = '{"type":"response","command":"steer","success":true}'
        event = RpcEvent(line, recv_ns=1)
        assert event.is_response_to("steer")
        assert not event.is_response_to("prompt")

    def test_failed_response_still_matches_its_command(self) -> None:
        line = '{"type":"response","command":"steer","success":false,"error":"x"}'
        assert RpcEvent(line, recv_ns=1).is_response_to("steer")

    def test_contains_searches_the_raw_line(self) -> None:
        line = json.dumps(
            {
                "type": "message_update",
                "message": {"content": [{"type": "text", "text": "STEERED-ACK"}]},
            }
        )
        event = RpcEvent(line, recv_ns=1)
        assert event.contains("STEERED-ACK")
        assert not event.contains("FOLLOWUP-ACK")


class TestTranscript:
    """The stamped in/out log every Arm 1 evidence file is written from."""

    def test_send_and_recv_entries_keep_order_and_stamps(self) -> None:
        transcript = Transcript()
        transcript.note_send('{"type":"prompt","message":"go"}', ns=10)
        transcript.note_recv('{"type":"agent_start"}', ns=20)
        dirs = [entry.direction for entry in transcript.entries()]
        stamps = [entry.ns for entry in transcript.entries()]
        assert dirs == ["send", "recv"]
        assert stamps == [10, 20]

    def test_dump_writes_sanitized_jsonl(self, tmp_path: Path) -> None:
        transcript = Transcript()
        secret_home = str(Path.home())
        transcript.note_send(
            f'{{"type":"prompt","message":"read {secret_home}"}}', ns=1
        )
        transcript.note_recv('{"type":"agent_end"}', ns=2)
        out = tmp_path / "transcript.jsonl"
        transcript.dump(out, Sanitizer.for_host())
        lines = out.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["dir"] == "send"
        assert first["ns"] == 1
        assert secret_home not in lines[0]
        assert "~" in first["data"]

    def test_events_returns_parsed_recv_entries_only(self) -> None:
        transcript = Transcript()
        transcript.note_send('{"type":"prompt","message":"go"}', ns=1)
        transcript.note_recv('{"type":"agent_start"}', ns=2)
        transcript.note_recv('{"type":"agent_end"}', ns=3)
        types = [event.type for event in transcript.events()]
        assert types == ["agent_start", "agent_end"]

    def test_events_skips_non_json_recv_lines(self) -> None:
        # The session layer records stray stdout prints in the transcript
        # on purpose; the analyzer must tolerate exactly what the session
        # tolerated instead of crashing the whole run at summary time.
        transcript = Transcript()
        transcript.note_recv("stray warning from the child", ns=1)
        transcript.note_recv('{"type":"agent_end"}', ns=2)
        types = [event.type for event in transcript.events()]
        assert types == ["agent_end"]
        # The raw line is still evidence: it stays in the entries.
        assert transcript.entries()[0].text == "stray warning from the child"
