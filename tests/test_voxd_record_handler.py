"""Tests for punt_vox.voxd.record_handler -- daemon-side store writes."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.websockets import WebSocketDisconnect

from punt_vox.voxd.record_handler import RecordHandler
from punt_vox.voxd.record_store import RecordStore
from punt_vox.voxd.synthesis_result import SynthesisOutcome


def _handler(
    store: RecordStore, source: Path, *, cached: bool = False
) -> RecordHandler:
    """Build a RecordHandler whose synthesis lands *source* (no real TTS)."""
    synth = MagicMock()
    synth.synthesize_to_file = AsyncMock(
        return_value=SynthesisOutcome(path=source, cached=cached)
    )
    return RecordHandler(synthesis=synth, store=store)


def _store(tmp_path: Path) -> RecordStore:
    return RecordStore(tmp_path / "recordings")


def _capturing_ws() -> tuple[MagicMock, list[dict[str, object]]]:
    """Return a mock websocket and the list of payloads it is sent."""
    sent: list[dict[str, object]] = []

    async def _send(payload: dict[str, object]) -> None:
        sent.append(payload)

    ws = MagicMock()
    ws.send_json = AsyncMock(side_effect=_send)
    return ws, sent


class TestRecordHandler:
    """The record handler stores the file daemon-side and returns its locator."""

    def test_long_record_delivers_byte_correct_file(self, tmp_path: Path) -> None:
        data = b"\xff\xfb\x90\x00" * 300_000  # ~1.2 MiB, over the old 1 MiB cap
        src = tmp_path / "src.mp3"
        src.write_bytes(data)
        store = _store(tmp_path)
        ws, sent = _capturing_ws()

        msg: dict[str, object] = {"type": "record", "id": "r1", "text": "hello"}
        asyncio.run(_handler(store, src)(msg, ws))

        assert sent[0]["type"] == "recording"  # ack precedes the file
        audio = next(p for p in sent if p["type"] == "audio")
        # The wire `path` is relativized (no absolute prefix); the file is located
        # by its bare name under the store root, not by reconstructing an abs path.
        assert not str(audio["path"]).startswith("/")
        landed = store.root / str(audio["name"])
        assert landed.parent == store.root  # contained in the store
        assert landed.read_bytes() == data
        assert audio["bytes"] == len(data) == landed.stat().st_size

    def test_record_returns_store_locator(self, tmp_path: Path) -> None:
        src = tmp_path / "src.mp3"
        src.write_bytes(b"\xff\xfb\x90\x00" * 8)
        store = _store(tmp_path)
        ws, sent = _capturing_ws()

        msg: dict[str, object] = {"type": "record", "id": "r1", "text": "hi"}
        asyncio.run(_handler(store, src)(msg, ws))

        audio = next(p for p in sent if p["type"] == "audio")
        assert "name" in audio and "path" in audio
        assert "data" not in audio  # no base64 audio frame crosses the wire

    @pytest.mark.parametrize("mib", [1, 3, 5])
    def test_arbitrarily_long_record_has_no_ceiling(
        self, tmp_path: Path, mib: int
    ) -> None:
        data = b"\x01" * (mib * 1024 * 1024)
        src = tmp_path / "src.mp3"
        src.write_bytes(data)
        store = _store(tmp_path)
        ws, sent = _capturing_ws()

        msg: dict[str, object] = {"type": "record", "id": "r1", "text": f"len{mib}"}
        asyncio.run(_handler(store, src)(msg, ws))

        audio = next(p for p in sent if p["type"] == "audio")
        assert audio["bytes"] == mib * 1024 * 1024

    def test_named_record_lands_under_that_name(self, tmp_path: Path) -> None:
        src = tmp_path / "src.mp3"
        src.write_bytes(b"\xff\xfb\x90\x00" * 4)
        store = _store(tmp_path)
        ws, sent = _capturing_ws()

        msg: dict[str, object] = {
            "type": "record",
            "id": "r1",
            "text": "hi",
            "name": "greeting.mp3",
        }
        asyncio.run(_handler(store, src)(msg, ws))

        audio = next(p for p in sent if p["type"] == "audio")
        assert audio["name"] == "greeting.mp3"
        assert Path(str(audio["path"])).name == "greeting.mp3"

    def test_client_close_mid_transfer_does_not_crash_daemon(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "src.mp3"
        src.write_bytes(b"\xff\xfb\x90\x00" * 10)
        # Ack succeeds; the terminal 'audio' reply hits a vanished client.
        ws = MagicMock()
        ws.send_json = AsyncMock(side_effect=[None, WebSocketDisconnect()])

        msg: dict[str, object] = {"type": "record", "id": "r1", "text": "hi"}
        # Must not raise -- a client disconnect is a normal end-of-request.
        asyncio.run(_handler(_store(tmp_path), src)(msg, ws))
        assert ws.send_json.await_count == 2

    def test_client_close_mid_transfer_daemon_serves_next_connection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After a client vanishes mid-record, a NEW connection still works."""
        from starlette.testclient import TestClient

        from punt_vox.voxd import build_app
        from punt_vox.voxd.synthesis import SynthesisPipeline

        async def fake_synth(
            _self: SynthesisPipeline, _text: str, _spec: object
        ) -> SynthesisOutcome:
            src = tmp_path / "synth.mp3"
            src.write_bytes(b"\xff\xfb\x90\x00" * 8)
            return SynthesisOutcome(path=src, cached=False)

        monkeypatch.setattr(SynthesisPipeline, "synthesize_to_file", fake_synth)

        app = build_app()
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws1:
                ws1.send_json({"type": "record", "id": "r1", "text": "hi"})
                assert ws1.receive_json()["type"] == "recording"
                # Exit the context = close mid-transfer, before the 'audio' frame.

            with client.websocket_connect("/ws") as ws2:
                ws2.send_json({"type": "health"})
                assert ws2.receive_json()["type"] == "health"

    def test_client_gone_before_ack_skips_synthesis(self, tmp_path: Path) -> None:
        """A client gone before the ack skips synthesis and writes no file."""
        synth = MagicMock()
        synth.synthesize_to_file = AsyncMock(
            return_value=SynthesisOutcome(path=tmp_path / "x.mp3", cached=False)
        )
        store = _store(tmp_path)
        handler = RecordHandler(synthesis=synth, store=store)
        ws = MagicMock()
        # The ack itself hits a vanished client.
        ws.send_json = AsyncMock(side_effect=WebSocketDisconnect())

        msg: dict[str, object] = {"type": "record", "id": "r1", "text": "hi"}
        asyncio.run(handler(msg, ws))

        synth.synthesize_to_file.assert_not_called()
        assert not store.root.exists() or not any(store.root.iterdir())

    def test_no_partial_file_on_synthesis_error(self, tmp_path: Path) -> None:
        synth = MagicMock()
        synth.synthesize_to_file = AsyncMock(side_effect=RuntimeError("boom"))
        store = _store(tmp_path)
        handler = RecordHandler(synthesis=synth, store=store)
        ws, sent = _capturing_ws()

        msg: dict[str, object] = {"type": "record", "id": "r1", "text": "hi"}
        asyncio.run(handler(msg, ws))

        error = next(p for p in sent if p["type"] == "error")
        # A non-OSError synthesis fault carries no relative form -- the wire gets
        # the generic verdict; the raw "boom" goes to the log via logger.exception.
        assert error["message"] == "operation failed"
        assert not store.root.exists() or not any(store.root.glob("*.mp3"))

    def test_empty_text_is_an_error(self, tmp_path: Path) -> None:
        src = tmp_path / "src.mp3"
        src.write_bytes(b"\x00")
        ws, sent = _capturing_ws()

        msg: dict[str, object] = {"type": "record", "id": "r1", "text": ""}
        asyncio.run(_handler(_store(tmp_path), src)(msg, ws))

        assert sent[-1]["type"] == "error"
        assert "empty text" in str(sent[-1]["message"])

    def test_non_string_voice_is_a_clean_error(self, tmp_path: Path) -> None:
        """A non-string typed field yields an error frame before the ack; the
        parse must not escape the handler and tear the connection down."""
        src = tmp_path / "src.mp3"
        src.write_bytes(b"\x00")
        ws, sent = _capturing_ws()

        msg: dict[str, object] = {
            "type": "record",
            "id": "r1",
            "text": "hi",
            "voice": 123,
        }
        asyncio.run(_handler(_store(tmp_path), src)(msg, ws))

        assert sent[-1]["type"] == "error"
        assert sent[-1]["id"] == "r1"
        assert "must be a string" in str(sent[-1]["message"])
        assert not any(p["type"] in ("recording", "audio") for p in sent)

    def test_non_string_text_is_a_clean_error(self, tmp_path: Path) -> None:
        """A non-string required text yields an error before the ack; 123 must
        not coerce to "123", the connection stays up, and no file lands."""
        src = tmp_path / "src.mp3"
        src.write_bytes(b"\x00")
        store = _store(tmp_path)
        ws, sent = _capturing_ws()

        msg: dict[str, object] = {"type": "record", "id": "r1", "text": 123}
        asyncio.run(_handler(store, src)(msg, ws))

        assert sent[-1]["type"] == "error"
        assert sent[-1]["id"] == "r1"
        assert "must be a string" in str(sent[-1]["message"])
        assert not any(p["type"] in ("recording", "audio") for p in sent)
        assert not store.root.exists() or not any(store.root.iterdir())

    def test_non_string_name_is_a_clean_error(self, tmp_path: Path) -> None:
        """A non-string name yields an error before the ack; 123 must not become
        the filename "123", the connection stays up, and no file lands."""
        src = tmp_path / "src.mp3"
        src.write_bytes(b"\x00")
        store = _store(tmp_path)
        ws, sent = _capturing_ws()

        msg: dict[str, object] = {
            "type": "record",
            "id": "r1",
            "text": "hi",
            "name": 123,
        }
        asyncio.run(_handler(store, src)(msg, ws))

        assert sent[-1]["type"] == "error"
        assert sent[-1]["id"] == "r1"
        assert "name must be a string" in str(sent[-1]["message"])
        assert not any(p["type"] in ("recording", "audio") for p in sent)
        assert not store.root.exists() or not any(store.root.iterdir())

    def test_empty_wire_name_rejected_before_ack(self, tmp_path: Path) -> None:
        """An explicit wire name "" is rejected, not silently content-addressed."""
        src = tmp_path / "src.mp3"
        src.write_bytes(b"\x00")
        ws, sent = _capturing_ws()

        msg: dict[str, object] = {
            "type": "record",
            "id": "r1",
            "text": "hi",
            "name": "",
        }
        asyncio.run(_handler(_store(tmp_path), src)(msg, ws))

        assert sent[-1]["type"] == "error"
        assert "empty" in str(sent[-1]["message"])
        assert not any(p["type"] in ("recording", "audio") for p in sent)

    def test_absolute_name_rejected_before_ack(self, tmp_path: Path) -> None:
        """An absolute name over the wire is refused before the ack (P1)."""
        src = tmp_path / "src.mp3"
        src.write_bytes(b"\x00")
        ws, sent = _capturing_ws()

        msg: dict[str, object] = {
            "type": "record",
            "id": "r1",
            "text": "hi",
            "name": "/etc/passwd",
        }
        asyncio.run(_handler(_store(tmp_path), src)(msg, ws))

        assert sent[-1]["type"] == "error"
        assert "absolute" in str(sent[-1]["message"])
        # Rejected before the ack -- no recording/audio frame was sent.
        assert not any(p["type"] in ("recording", "audio") for p in sent)

    def test_traversal_name_rejected_before_ack(self, tmp_path: Path) -> None:
        """A traversing name over the wire is refused before the ack (P1)."""
        src = tmp_path / "src.mp3"
        src.write_bytes(b"\x00")
        ws, sent = _capturing_ws()

        msg: dict[str, object] = {
            "type": "record",
            "id": "r1",
            "text": "hi",
            "name": "../../etc/cron.d/x",
        }
        asyncio.run(_handler(_store(tmp_path), src)(msg, ws))

        assert sent[-1]["type"] == "error"
        assert not any(p["type"] in ("recording", "audio") for p in sent)

    def test_nul_name_is_a_clean_error(self, tmp_path: Path) -> None:
        """A NUL-bearing name yields an error frame, never a crash or hang."""
        src = tmp_path / "src.mp3"
        src.write_bytes(b"\x00")
        ws, sent = _capturing_ws()

        msg: dict[str, object] = {
            "type": "record",
            "id": "r1",
            "text": "hi",
            "name": "bad\x00name.mp3",
        }
        # Must not raise -- a malformed name is a clean error, never a hang.
        asyncio.run(_handler(_store(tmp_path), src)(msg, ws))

        assert any(p["type"] == "error" for p in sent)
        assert not any(p["type"] == "audio" for p in sent)

    def test_rejected_hostile_name_logs_one_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A blocked hostile name emits exactly one audit WARNING with the id.

        The rejection is the security-relevant event: an operator grepping
        vox.log must see the blocked probe. Containment behavior is unchanged --
        the error frame still names the reason and no ack/audio is sent.
        """
        src = tmp_path / "src.mp3"
        src.write_bytes(b"\x00")
        ws, sent = _capturing_ws()

        msg: dict[str, object] = {
            "type": "record",
            "id": "rej-9",
            "text": "hi",
            "name": "/etc/passwd",
        }
        with caplog.at_level(logging.WARNING):
            asyncio.run(_handler(_store(tmp_path), src)(msg, ws))

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "rej-9" in warnings[0].getMessage()
        assert sent[-1]["type"] == "error"
        assert "absolute" in str(sent[-1]["message"])

    def test_control_char_name_rejected_and_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A newline-bearing name is refused, audit-logged, and never echoed raw.

        The rejection emits one WARNING; the sanitizer escapes the newline so no
        raw control character reaches the log record (log-injection defense).
        """
        src = tmp_path / "src.mp3"
        src.write_bytes(b"\x00")
        ws, sent = _capturing_ws()

        msg: dict[str, object] = {
            "type": "record",
            "id": "rej-nl",
            "text": "hi",
            "name": "bad\nINJECTED.mp3",
        }
        with caplog.at_level(logging.WARNING):
            asyncio.run(_handler(_store(tmp_path), src)(msg, ws))

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "rej-nl" in warnings[0].getMessage()
        assert "\n" not in warnings[0].getMessage()  # sanitized, no raw newline
        assert sent[-1]["type"] == "error"
        assert "non-printable" in str(sent[-1]["message"])
        assert not any(p["type"] in ("recording", "audio") for p in sent)

    def test_successful_record_logs_info_not_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A clean record logs its INFO line and emits no rejection WARNING."""
        src = tmp_path / "src.mp3"
        src.write_bytes(b"\xff\xfb\x90\x00" * 8)
        ws, _sent = _capturing_ws()

        msg: dict[str, object] = {"type": "record", "id": "r1", "text": "hi"}
        with caplog.at_level(logging.INFO):
            asyncio.run(_handler(_store(tmp_path), src)(msg, ws))

        assert any(
            r.levelno == logging.INFO and "Record:" in r.getMessage()
            for r in caplog.records
        )
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]
