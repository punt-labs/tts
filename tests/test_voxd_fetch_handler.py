"""Tests for punt_vox.voxd.fetch_handler -- chunked store/album-part retrieval.

The handler resolves a bare recording ``ref`` or a catalog ``album`` id + bare
``part`` name once (containment-checked before the first byte), then delegates to
``ChunkedTransfer``. These tests assert the resolution and the reused containment
+ audit invariants AT THE FETCH OP: a hostile ref/part is refused before any
``fetch_begin`` and audit-logged (F2 -- the album id is a catalog key, the part a
validated name); a not-found ref/album is a clean error.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import TYPE_CHECKING, cast, final

from punt_vox.voxd.fetch_handler import FetchHandler
from punt_vox.voxd.programs.catalog import Catalog
from punt_vox.voxd.programs.filesystem_store import FilesystemProgramStore
from punt_vox.voxd.programs.library import MusicLibrary
from punt_vox.voxd.programs.part import Part
from punt_vox.voxd.record_store import RecordStore

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from starlette.websockets import WebSocket

    from punt_vox.voxd.programs.producer import PartSpec


@final
class _QuietProducer:
    """Write a byte to the target and return a ready Part."""

    __slots__ = ()

    async def produce(self, spec: PartSpec, target: Path) -> Part:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"audio")
        return Part(target.name, spec.index)


def _capturing_ws() -> tuple[WebSocket, list[dict[str, object]]]:
    sent: list[dict[str, object]] = []

    @final
    class _WS:
        async def send_json(self, payload: dict[str, object]) -> None:
            sent.append(payload)

    return cast("WebSocket", _WS()), sent


def _handler(tmp_path: Path) -> tuple[FetchHandler, RecordStore, MusicLibrary]:
    store = RecordStore(tmp_path / "recordings")
    store.root.mkdir(parents=True)
    programs = tmp_path / "programs"
    prog_store = FilesystemProgramStore(programs)
    lib = MusicLibrary(
        Catalog(prog_store.scan()), prog_store, programs, _QuietProducer()
    )
    return FetchHandler(store=store, music=lib), store, lib


def _reassemble(frames: list[dict[str, object]]) -> bytes:
    data = b""
    for frame in frames:
        if frame["type"] == "chunk":
            data += base64.b64decode(str(frame["data"]))
    return data


class TestRecordingFetch:
    def test_streams_a_recording(self, tmp_path: Path) -> None:
        handler, store, _ = _handler(tmp_path)
        blob = b"\xff\xfb\x90\x00" * 100
        (store.root / "a1b2c3.mp3").write_bytes(blob)
        ws, sent = _capturing_ws()
        msg: dict[str, object] = {"id": "f1", "ref": "a1b2c3.mp3"}
        asyncio.run(handler(msg, ws))

        assert sent[0]["type"] == "fetch_begin"
        assert sent[0]["ref"] == "a1b2c3.mp3"
        assert sent[-1]["type"] == "fetch_end"
        assert _reassemble(sent) == blob

    def test_unknown_recording_is_an_error(self, tmp_path: Path) -> None:
        handler, _, _ = _handler(tmp_path)
        ws, sent = _capturing_ws()
        msg: dict[str, object] = {"id": "f1", "ref": "nope.mp3"}
        asyncio.run(handler(msg, ws))

        assert sent[-1]["type"] == "error"
        assert "no recording" in str(sent[-1]["message"])
        # No fetch_begin was ever sent for a missing recording.
        assert not [f for f in sent if f["type"] == "fetch_begin"]

    def test_hostile_ref_refused_before_begin_and_audited(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        handler, _, _ = _handler(tmp_path)
        ws, sent = _capturing_ws()
        msg: dict[str, object] = {"id": "f8", "ref": "../../etc/passwd"}
        with caplog.at_level(logging.WARNING):
            asyncio.run(handler(msg, ws))

        assert sent[-1]["type"] == "error"
        assert not [f for f in sent if f["type"] == "fetch_begin"]
        assert any("f8" in r.getMessage() for r in caplog.records)

    def test_missing_ref_and_album_is_an_error(self, tmp_path: Path) -> None:
        handler, _, _ = _handler(tmp_path)
        ws, sent = _capturing_ws()
        asyncio.run(handler({"id": "f1"}, ws))
        assert sent[-1]["type"] == "error"

    def test_non_string_album_is_a_clean_error(self, tmp_path: Path) -> None:
        """A non-string ``album`` yields an error frame -- the parse must not
        escape the handler and tear the connection down."""
        handler, _, _ = _handler(tmp_path)
        ws, sent = _capturing_ws()
        asyncio.run(handler({"id": "f1", "album": 123}, ws))
        assert sent[-1]["type"] == "error"
        assert "must be a string" in str(sent[-1]["message"])
        assert not [f for f in sent if f["type"] == "fetch_begin"]


class TestMusicPartFetch:
    def test_streams_an_album_part(self, tmp_path: Path) -> None:
        handler, _, lib = _handler(tmp_path)
        album_id = asyncio.run(lib.new("a prompt", None)).value
        ws, sent = _capturing_ws()
        msg: dict[str, object] = {"id": "f1", "album": album_id, "part": "001.mp3"}
        asyncio.run(handler(msg, ws))

        assert sent[0]["type"] == "fetch_begin"
        assert sent[0]["ref"] == "001.mp3"
        assert sent[-1]["type"] == "fetch_end"
        assert _reassemble(sent) == b"audio"

    def test_unknown_album_is_an_error(self, tmp_path: Path) -> None:
        handler, _, _ = _handler(tmp_path)
        ws, sent = _capturing_ws()
        msg: dict[str, object] = {"id": "f1", "album": "abcdef", "part": "001.mp3"}
        asyncio.run(handler(msg, ws))

        assert sent[-1]["type"] == "error"
        assert "no album named" in str(sent[-1]["message"])
        assert not [f for f in sent if f["type"] == "fetch_begin"]

    def test_hostile_part_name_refused_and_audited(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A part-name escape is refused inside the resolved album dir (F2)."""
        handler, _, lib = _handler(tmp_path)
        album_id = asyncio.run(lib.new("a prompt", None)).value
        ws, sent = _capturing_ws()
        msg: dict[str, object] = {"id": "f9", "album": album_id, "part": "../../etc/x"}
        with caplog.at_level(logging.WARNING):
            asyncio.run(handler(msg, ws))

        assert sent[-1]["type"] == "error"
        assert not [f for f in sent if f["type"] == "fetch_begin"]
        assert any("f9" in r.getMessage() for r in caplog.records)

    def test_album_without_part_is_an_error(self, tmp_path: Path) -> None:
        handler, _, lib = _handler(tmp_path)
        album_id = asyncio.run(lib.new("a prompt", None)).value
        ws, sent = _capturing_ws()
        msg: dict[str, object] = {"id": "f1", "album": album_id}
        asyncio.run(handler(msg, ws))
        assert sent[-1]["type"] == "error"
        assert "requires a part" in str(sent[-1]["message"])
