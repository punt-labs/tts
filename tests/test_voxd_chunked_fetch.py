"""Tests for punt_vox.voxd.chunked_fetch -- the ordered, bounded get transport.

These assert the properties of ``docs/vox-chunked-transfer.tex`` by name against a
real file under ``tmp_path`` and a fake WebSocket capturing frames, driving
``ChunkedTransfer`` with a small chunk bound so multi-chunk streams are cheap:
ordered-contiguous seq, the reassembled total equal to the declared bytes, the
empty-file case, the sha256 the client verifies, and the atomic terminal -- a
mid-stream fault ends with an ``error`` frame and no ``fetch_end``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
from typing import TYPE_CHECKING, final

import pytest
from starlette.websockets import WebSocketDisconnect

from punt_vox.voxd.chunked_fetch import ChunkedTransfer
from punt_vox.voxd.wire_reply import WireReply

if TYPE_CHECKING:
    from pathlib import Path

_CHUNK = 4  # a tiny bound so a handful of bytes spans several frames


def _capturing_ws() -> tuple[object, list[dict[str, object]]]:
    sent: list[dict[str, object]] = []

    @final
    class _WS:
        async def send_json(self, payload: dict[str, object]) -> None:
            sent.append(payload)

    return _WS(), sent


def _run(path: Path, ref: str, chunk: int = _CHUNK) -> list[dict[str, object]]:
    ws, sent = _capturing_ws()
    reply = WireReply(ws, "f1")  # type: ignore[arg-type]
    asyncio.run(ChunkedTransfer(reply, chunk).stream(path, ref))
    return sent


def _stub_two_passes(
    monkeypatch: pytest.MonkeyPatch, path: Path, measured: bytes, sent: bytes
) -> None:
    """Make ``path.open`` return ``measured`` bytes on the 1st call, ``sent`` after.

    Models a file that changed between the measure pass and the send pass, so the
    two passes see different content without racing the real filesystem.
    """
    calls = {"n": 0}

    def two_files(self: Path, *args: object, **kwargs: object) -> object:
        del self, args, kwargs
        calls["n"] += 1
        return io.BytesIO(measured if calls["n"] == 1 else sent)

    monkeypatch.setattr(type(path), "open", two_files)


def _reassemble(frames: list[dict[str, object]]) -> bytes:
    """Walk begin -> chunk* -> end, returning the reassembled bytes."""
    assert frames[0]["type"] == "fetch_begin"
    assert frames[-1]["type"] == "fetch_end"
    data = b""
    for frame in frames[1:-1]:
        assert frame["type"] == "chunk"
        data += base64.b64decode(str(frame["data"]))
    return data


class TestChunkBoundValidation:
    def test_positive_bound_constructs(self) -> None:
        """A positive chunk bound instantiates the transport."""
        ws, _ = _capturing_ws()
        reply = WireReply(ws, "f1")  # type: ignore[arg-type]
        assert ChunkedTransfer(reply, 1) is not None

    def test_zero_bound_rejected(self) -> None:
        """A zero chunk bound is refused -- it would divide by zero in _measure."""
        ws, _ = _capturing_ws()
        reply = WireReply(ws, "f1")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="chunk_bytes must be positive"):
            ChunkedTransfer(reply, 0)

    def test_negative_bound_rejected(self) -> None:
        """A negative chunk bound is refused -- it breaks the bounded-memory read."""
        ws, _ = _capturing_ws()
        reply = WireReply(ws, "f1")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="chunk_bytes must be positive"):
            ChunkedTransfer(reply, -1)


class TestOrderedDelivery:
    def test_ordered_contiguous_seq(self, tmp_path: Path) -> None:
        """Every chunk carries the next seq: 0, 1, ... with no gap or repeat."""
        path = tmp_path / "r.mp3"
        path.write_bytes(b"abcdefghij")  # 10 bytes, chunk 4 -> 3 frames
        frames = _run(path, "r.mp3")
        seqs = [f["seq"] for f in frames if f["type"] == "chunk"]
        assert seqs == [0, 1, 2]

    def test_each_chunk_within_bound(self, tmp_path: Path) -> None:
        path = tmp_path / "r.mp3"
        path.write_bytes(b"abcdefghij")
        frames = _run(path, "r.mp3")
        for frame in frames:
            if frame["type"] == "chunk":
                assert len(base64.b64decode(str(frame["data"]))) <= _CHUNK

    def test_total_equals_declared(self, tmp_path: Path) -> None:
        """fetch_begin/end declare the byte count and it equals the reassembly."""
        path = tmp_path / "r.mp3"
        blob = b"\xff\xfb\x90\x00" * 1000  # 4000 bytes, many chunks
        path.write_bytes(blob)
        frames = _run(path, "r.mp3")
        assert frames[0]["bytes"] == len(blob)
        assert frames[-1]["bytes"] == len(blob)
        assert _reassemble(frames) == blob

    def test_reassembles_any_size(self, tmp_path: Path) -> None:
        """A file spanning 1, 2, and many chunks round-trips byte-correct."""
        for size in (1, _CHUNK, _CHUNK + 1, 5 * _CHUNK + 3):
            path = tmp_path / f"r{size}.mp3"
            blob = bytes(range(256)) * size
            path.write_bytes(blob[:size])
            frames = _run(path, path.name)
            assert _reassemble(frames) == blob[:size]

    def test_empty_file_begins_and_ends_with_no_chunks(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.mp3"
        path.write_bytes(b"")
        frames = _run(path, "empty.mp3")
        assert frames[0]["type"] == "fetch_begin"
        assert frames[0]["bytes"] == 0
        assert frames[0]["chunks"] == 0
        assert frames[-1]["type"] == "fetch_end"
        assert [f for f in frames if f["type"] == "chunk"] == []

    def test_sha256_matches_content(self, tmp_path: Path) -> None:
        path = tmp_path / "r.mp3"
        blob = b"the quick brown fox"
        path.write_bytes(blob)
        frames = _run(path, "r.mp3")
        assert frames[0]["sha256"] == hashlib.sha256(blob).hexdigest()


class TestAtomicTerminal:
    def test_mid_stream_fault_ends_with_error_not_end(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A read fault after fetch_begin ends with an error terminal, no fetch_end.

        The measure pass opens the file once; the send pass opens it again. Failing
        the second open models the file vanishing mid-stream: begin is announced,
        then an error terminal, so the client discards its partial (no complete
        file is ever claimed).
        """
        path = tmp_path / "r.mp3"
        path.write_bytes(b"abcdefghij")
        contents = path.read_bytes()
        calls = {"n": 0}

        def flaky_open(self: Path, *args: object, **kwargs: object) -> object:
            del self, args, kwargs
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("vanished mid-stream")
            return io.BytesIO(contents)

        monkeypatch.setattr(type(path), "open", flaky_open)
        frames = _run(path, "r.mp3")

        assert frames[0]["type"] == "fetch_begin"
        assert frames[-1]["type"] == "error"
        assert not [f for f in frames if f["type"] == "fetch_end"]

    def test_measure_fault_rejects_before_begin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fault while measuring is refused before any fetch_begin or chunk."""
        path = tmp_path / "r.mp3"
        path.write_bytes(b"abcdefghij")

        def boom(self: Path, *args: object, **kwargs: object) -> object:
            raise OSError("cannot open")

        monkeypatch.setattr(type(path), "open", boom)
        frames = _run(path, "r.mp3")

        assert [f["type"] for f in frames] == ["error"]  # no begin, no chunk

    def test_shrunk_file_aborts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file that yields fewer bytes than declared aborts, never a false end.

        The measure pass sees 10 bytes; the send pass sees a shrunk 3, so the
        stream ends early with ``sent < declared`` and aborts.
        """
        path = tmp_path / "r.mp3"
        path.write_bytes(b"abcdefghij")
        _stub_two_passes(monkeypatch, path, b"abcdefghij", b"abc")
        frames = _run(path, "r.mp3")

        assert frames[0]["type"] == "fetch_begin"
        assert frames[0]["bytes"] == 10
        assert frames[-1]["type"] == "error"
        assert not [f for f in frames if f["type"] == "fetch_end"]


class TestPeerGoneMidStream:
    def test_pump_stops_encoding_when_peer_disconnects(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A chunk send that finds the peer gone stops the pump at once.

        The next chunk is never read or base64-encoded, and the exchange ends
        silently -- no ``fetch_end`` and no ``error`` terminal, since a normal
        disconnect is not a fault. Without the send-result check the pump would
        keep encoding every remaining chunk into a socket nobody is reading.
        """
        path = tmp_path / "r.mp3"
        path.write_bytes(b"abcdefghij")  # 10 bytes, chunk 4 -> 3 chunks

        encodes = {"n": 0}
        real_b64 = base64.b64encode

        def counting_b64(data: bytes) -> bytes:
            encodes["n"] += 1
            return real_b64(data)

        monkeypatch.setattr(
            "punt_vox.voxd.chunked_fetch.base64.b64encode", counting_b64
        )

        sent: list[dict[str, object]] = []

        @final
        class _GoneAfterFirstChunk:
            async def send_json(self, payload: dict[str, object]) -> None:
                sent.append(payload)
                # The peer drops once the second chunk send is attempted.
                if sum(1 for f in sent if f.get("type") == "chunk") >= 2:
                    raise WebSocketDisconnect(code=1006)

        reply = WireReply(_GoneAfterFirstChunk(), "f1")  # type: ignore[arg-type]
        asyncio.run(ChunkedTransfer(reply, _CHUNK).stream(path, "r.mp3"))

        # Chunk 0 (delivered) and chunk 1 (the send that finds the gone peer) are
        # encoded; the pump then stops, so chunk 2 is never encoded.
        assert encodes["n"] == 2
        assert not [f for f in sent if f["type"] == "fetch_end"]
        assert not [f for f in sent if f["type"] == "error"]


class TestGrownFile:
    def test_grown_file_transfers_declared_prefix_bounded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file grown after measure still transfers exactly its declared prefix.

        The measure pass sees 8 bytes; the send pass sees a grown 20. The stream
        is bounded by the declaration -- exactly the first 8 bytes are sent, never
        the larger on-disk size.
        """
        path = tmp_path / "r.mp3"
        path.write_bytes(b"abcdefgh")
        _stub_two_passes(monkeypatch, path, b"abcdefgh", b"abcdefgh" + b"X" * 12)
        frames = _run(path, "r.mp3")

        assert frames[0]["bytes"] == 8
        assert frames[-1]["type"] == "fetch_end"
        assert _reassemble(frames) == b"abcdefgh"


class TestAudit:
    """A read fault is a server-side operational failure, audited as a fault."""

    def test_measure_fault_audits_operation_failed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A fault before fetch_begin routes through fault: ERROR "operation failed"."""
        path = tmp_path / "r.mp3"
        path.write_bytes(b"abc")

        def boom(self: Path, *args: object, **kwargs: object) -> object:
            raise OSError("nope")

        monkeypatch.setattr(type(path), "open", boom)
        with caplog.at_level(logging.WARNING, logger="punt_vox.voxd.wire_reply"):
            _run(path, "r.mp3")
        assert any(
            r.levelno == logging.ERROR
            and "operation failed" in r.getMessage()
            and "f1" in r.getMessage()
            for r in caplog.records
        )
        assert not any("rejected op" in r.getMessage() for r in caplog.records)

    def test_mid_stream_fault_audits_operation_failed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A fault after fetch_begin also audits as a fault, never a rejection."""
        path = tmp_path / "r.mp3"
        path.write_bytes(b"abcdefghij")
        contents = path.read_bytes()
        calls = {"n": 0}

        def flaky_open(self: Path, *args: object, **kwargs: object) -> object:
            del self, args, kwargs
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("vanished mid-stream")
            return io.BytesIO(contents)

        monkeypatch.setattr(type(path), "open", flaky_open)
        with caplog.at_level(logging.WARNING, logger="punt_vox.voxd.wire_reply"):
            _run(path, "r.mp3")
        assert any(
            r.levelno == logging.ERROR and "operation failed" in r.getMessage()
            for r in caplog.records
        )
        assert not any("rejected op" in r.getMessage() for r in caplog.records)
