"""Tests for punt_vox.voxd.play_handler -- daemon-host playback of store files."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from punt_vox.voxd.play_handler import PlayHandler
from punt_vox.voxd.playback import PlaybackItem, PlaybackResult
from punt_vox.voxd.record_store import RecordStore

if TYPE_CHECKING:
    import pytest


def _ok_result(path: Path) -> PlaybackResult:
    """A playback result that reads as success (clean exit, real duration)."""
    return PlaybackResult(path=path, rc=0, elapsed_s=1.2, stderr="", ts=0.0)


def _failed_result(path: Path) -> PlaybackResult:
    """A playback result that reads as failure (player exited non-zero)."""
    return PlaybackResult(
        path=path, rc=1, elapsed_s=0.0, stderr="no player found", ts=0.0
    )


def _playback_with(result_for: object) -> MagicMock:
    """A PlaybackQueue mock whose enqueue resolves the item outcome with *result_for*.

    *result_for* is a callable ``(path) -> PlaybackResult`` so a test can make
    the host-side playback report success or failure.
    """

    async def _enqueue(item: PlaybackItem) -> None:
        item.notify.set()
        if item.outcome is not None and not item.outcome.done():
            item.outcome.set_result(result_for(item.path))  # type: ignore[operator]

    playback = MagicMock()
    playback.enqueue = AsyncMock(side_effect=_enqueue)
    return playback


def _playback_that_completes() -> MagicMock:
    """A PlaybackQueue mock whose playback succeeds."""
    return _playback_with(_ok_result)


def _capturing_ws() -> tuple[MagicMock, list[dict[str, object]]]:
    sent: list[dict[str, object]] = []

    async def _send(payload: dict[str, object]) -> None:
        sent.append(payload)

    ws = MagicMock()
    ws.send_json = AsyncMock(side_effect=_send)
    return ws, sent


class TestPlayHandler:
    """play resolves a store ref and plays it on the daemon, not the client."""

    def test_play_routes_through_daemon(self, tmp_path: Path) -> None:
        store = RecordStore(tmp_path / "recordings")
        store.root.mkdir(parents=True)
        rec = store.root / "a1b2c3.mp3"
        rec.write_bytes(b"\xff\xfb\x90\x00" * 4)
        playback = _playback_that_completes()
        ws, sent = _capturing_ws()

        msg: dict[str, object] = {"type": "play", "id": "p1", "ref": "a1b2c3.mp3"}
        asyncio.run(PlayHandler(playback=playback, store=store)(msg, ws))

        playback.enqueue.assert_awaited_once()
        item = playback.enqueue.await_args.args[0]
        assert isinstance(item, PlaybackItem)
        assert item.path == rec.resolve()  # the in-store path, played daemon-side
        assert item.outcome is not None  # the handler awaits the real outcome
        assert [p["type"] for p in sent] == ["playing", "done"]

    def test_host_side_failure_surfaces_error(self, tmp_path: Path) -> None:
        """A failed host-side playback reaches the client as an error, not a done."""
        store = RecordStore(tmp_path / "recordings")
        store.root.mkdir(parents=True)
        (store.root / "a1b2c3.mp3").write_bytes(b"\xff\xfb\x90\x00" * 4)
        playback = _playback_with(_failed_result)
        ws, sent = _capturing_ws()

        msg: dict[str, object] = {"type": "play", "id": "p1", "ref": "a1b2c3.mp3"}
        asyncio.run(PlayHandler(playback=playback, store=store)(msg, ws))

        assert [p["type"] for p in sent] == ["playing", "error"]
        assert "playback failed" in str(sent[-1]["message"])
        assert "no player found" in str(sent[-1]["message"])

    def test_played_nothing_surfaces_error(self, tmp_path: Path) -> None:
        """A clean exit under the suspicious-elapsed floor is a failure, not success."""
        store = RecordStore(tmp_path / "recordings")
        store.root.mkdir(parents=True)
        (store.root / "a1b2c3.mp3").write_bytes(b"\xff\xfb\x90\x00" * 4)

        def _noop(path: Path) -> PlaybackResult:
            return PlaybackResult(path=path, rc=0, elapsed_s=0.001, stderr="", ts=0.0)

        playback = _playback_with(_noop)
        ws, sent = _capturing_ws()

        msg: dict[str, object] = {"type": "play", "id": "p1", "ref": "a1b2c3.mp3"}
        asyncio.run(PlayHandler(playback=playback, store=store)(msg, ws))

        assert sent[-1]["type"] == "error"
        assert "played nothing" in str(sent[-1]["message"])

    def test_play_ref_outside_root_rejected(self, tmp_path: Path) -> None:
        store = RecordStore(tmp_path / "recordings")
        playback = _playback_that_completes()
        ws, sent = _capturing_ws()

        msg: dict[str, object] = {"type": "play", "id": "p1", "ref": "/etc/passwd"}
        asyncio.run(PlayHandler(playback=playback, store=store)(msg, ws))

        assert sent[-1]["type"] == "error"
        assert "absolute" in str(sent[-1]["message"])
        playback.enqueue.assert_not_awaited()

    def test_play_traversal_ref_rejected(self, tmp_path: Path) -> None:
        store = RecordStore(tmp_path / "recordings")
        playback = _playback_that_completes()
        ws, sent = _capturing_ws()

        msg: dict[str, object] = {"type": "play", "id": "p1", "ref": "../../etc/x"}
        asyncio.run(PlayHandler(playback=playback, store=store)(msg, ws))

        assert sent[-1]["type"] == "error"
        playback.enqueue.assert_not_awaited()

    def test_missing_ref_is_an_error(self, tmp_path: Path) -> None:
        store = RecordStore(tmp_path / "recordings")
        playback = _playback_that_completes()
        ws, sent = _capturing_ws()

        msg: dict[str, object] = {"type": "play", "id": "p1"}
        asyncio.run(PlayHandler(playback=playback, store=store)(msg, ws))

        assert sent[-1]["type"] == "error"
        assert "requires a ref" in str(sent[-1]["message"])

    def test_non_string_ref_is_a_clean_error(self, tmp_path: Path) -> None:
        """A non-string ``ref`` yields an error frame; the parse must not escape
        the handler and tear the connection down."""
        store = RecordStore(tmp_path / "recordings")
        playback = _playback_that_completes()
        ws, sent = _capturing_ws()

        msg: dict[str, object] = {"type": "play", "id": "p1", "ref": 123}
        asyncio.run(PlayHandler(playback=playback, store=store)(msg, ws))

        assert sent[-1]["type"] == "error"
        assert "must be a string" in str(sent[-1]["message"])
        playback.enqueue.assert_not_awaited()

    def test_unknown_recording_is_an_error(self, tmp_path: Path) -> None:
        """A well-formed ref that does not exist in the store is refused."""
        store = RecordStore(tmp_path / "recordings")
        store.root.mkdir(parents=True)
        playback = _playback_that_completes()
        ws, sent = _capturing_ws()

        msg: dict[str, object] = {"type": "play", "id": "p1", "ref": "nope.mp3"}
        asyncio.run(PlayHandler(playback=playback, store=store)(msg, ws))

        assert sent[-1]["type"] == "error"
        assert "no recording" in str(sent[-1]["message"])
        playback.enqueue.assert_not_awaited()

    def test_client_disconnect_on_ack_does_not_raise(self, tmp_path: Path) -> None:
        """A client gone when the 'playing' ack is sent ends the request quietly.

        The recording was already enqueued (it still plays on the host); the
        disconnect must not escape as a router traceback.
        """
        from starlette.websockets import WebSocketDisconnect

        store = RecordStore(tmp_path / "recordings")
        store.root.mkdir(parents=True)
        (store.root / "a1b2c3.mp3").write_bytes(b"\xff\xfb\x90\x00" * 4)
        playback = _playback_that_completes()
        ws = MagicMock()
        ws.send_json = AsyncMock(side_effect=WebSocketDisconnect())

        msg: dict[str, object] = {"type": "play", "id": "p1", "ref": "a1b2c3.mp3"}
        # Must not raise -- a normal disconnect is a quiet end-of-request.
        asyncio.run(PlayHandler(playback=playback, store=store)(msg, ws))
        playback.enqueue.assert_awaited_once()

    def test_unknown_recording_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A not-found ref is audit-logged once at WARNING with the request id."""
        store = RecordStore(tmp_path / "recordings")
        store.root.mkdir(parents=True)
        playback = _playback_that_completes()
        ws, _sent = _capturing_ws()

        msg: dict[str, object] = {"type": "play", "id": "p7", "ref": "nope.mp3"}
        with caplog.at_level(logging.WARNING):
            asyncio.run(PlayHandler(playback=playback, store=store)(msg, ws))

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "p7" in warnings[0].getMessage()
        playback.enqueue.assert_not_awaited()

    def test_successful_play_logs_info_not_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A clean play logs its INFO line and emits no rejection WARNING."""
        store = RecordStore(tmp_path / "recordings")
        store.root.mkdir(parents=True)
        (store.root / "a1b2c3.mp3").write_bytes(b"\xff\xfb\x90\x00" * 4)
        playback = _playback_that_completes()
        ws, _sent = _capturing_ws()

        msg: dict[str, object] = {"type": "play", "id": "p1", "ref": "a1b2c3.mp3"}
        with caplog.at_level(logging.INFO):
            asyncio.run(PlayHandler(playback=playback, store=store)(msg, ws))

        assert any(
            r.levelno == logging.INFO and "Play:" in r.getMessage()
            for r in caplog.records
        )
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]
