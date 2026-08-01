"""Tests for the mpv IPC value types -- command, response, event, end-file reason."""

from __future__ import annotations

import json

import pytest

from punt_vox.types_programs.mpv_event import (
    EndFileReason,
    MpvCommand,
    MpvEvent,
    MpvResponse,
)
from punt_vox.types_programs.wire import JsonObject


class TestEndFileReason:
    def test_only_eof_and_error_advance(self) -> None:
        # A natural end or a bad file drives the loop; teardown and crash do not.
        assert EndFileReason.EOF.advances is True
        assert EndFileReason.ERROR.advances is True
        assert EndFileReason.STOP.advances is False
        assert EndFileReason.REDIRECT.advances is False
        assert EndFileReason.QUIT.advances is False
        assert EndFileReason.CRASHED.advances is False

    def test_from_wire_keeps_a_known_reason(self) -> None:
        assert EndFileReason.from_wire("error") is EndFileReason.ERROR
        assert EndFileReason.from_wire("stop") is EndFileReason.STOP
        assert EndFileReason.from_wire("eof") is EndFileReason.EOF

    def test_from_wire_folds_an_unrecognized_reason_to_advancing_eof(self) -> None:
        # A newer mpv can emit ``unknown``; folding it to the advancing eof class
        # lets the loop advance rather than hang on an end-file it cannot classify.
        assert EndFileReason.from_wire("unknown") is EndFileReason.EOF
        assert EndFileReason.from_wire("nonsense") is EndFileReason.EOF
        assert EndFileReason.from_wire("unknown").advances is True


class TestMpvCommand:
    def test_loadfile_is_the_three_element_replace_form(self) -> None:
        # Version-robust: the paused flag rides the global pause property, not a
        # per-file option, so loadfile stays three elements at the pinned minimum.
        assert MpvCommand.loadfile("/m/1.mp3").args == (
            "loadfile",
            "/m/1.mp3",
            "replace",
        )

    def test_set_pause_carries_the_bool(self) -> None:
        assert MpvCommand.set_pause(paused=True).args == ("set_property", "pause", True)
        assert MpvCommand.set_pause(paused=False).args == (
            "set_property",
            "pause",
            False,
        )

    def test_stop_and_quit(self) -> None:
        assert MpvCommand.stop().args == ("stop",)
        assert MpvCommand.quit().args == ("quit",)

    def test_framed_carries_the_request_id_as_newline_json(self) -> None:
        frame = MpvCommand.loadfile("/m/1.mp3").framed(7)
        assert frame.endswith(b"\n")
        payload = json.loads(frame)
        assert payload == {
            "command": ["loadfile", "/m/1.mp3", "replace"],
            "request_id": 7,
        }


class TestMpvResponse:
    def test_ok_only_when_error_is_success(self) -> None:
        obj = JsonObject.coerce({"request_id": 3, "error": "success"}, "r")
        response = MpvResponse.from_object(obj)
        assert response.request_id == 3
        assert response.ok is True

    def test_a_non_success_error_is_not_ok(self) -> None:
        obj = JsonObject.coerce({"request_id": 4, "error": "property not found"}, "r")
        assert MpvResponse.from_object(obj).ok is False


class TestMpvEvent:
    def test_end_file_carries_its_reason(self) -> None:
        obj = JsonObject.coerce({"event": "end-file", "reason": "eof"}, "e")
        event = MpvEvent.from_object(obj)
        assert event.name == "end-file"
        assert event.reason is EndFileReason.EOF

    def test_a_non_end_file_event_has_no_reason(self) -> None:
        obj = JsonObject.coerce({"event": "start-file"}, "e")
        event = MpvEvent.from_object(obj)
        assert event.name == "start-file"
        assert event.reason is None

    def test_an_unrecognized_end_file_reason_folds_to_advancing_eof(self) -> None:
        # A newer mpv can emit a reason this enum does not name (``unknown``);
        # from_object folds it to the advancing eof class so the loop advances
        # rather than hanging on an end-file it cannot classify.
        obj = JsonObject.coerce({"event": "end-file", "reason": "bogus"}, "e")
        event = MpvEvent.from_object(obj)
        assert event.reason is EndFileReason.EOF
        assert event.reason.advances is True

    def test_an_end_file_missing_its_reason_raises(self) -> None:
        # A genuinely malformed end-file (no reason field) still raises -- the
        # reader drops the line rather than resolving a bogus outcome.
        obj = JsonObject.coerce({"event": "end-file"}, "e")
        with pytest.raises(ValueError, match="reason"):
            MpvEvent.from_object(obj)
