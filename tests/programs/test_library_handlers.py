"""Tests for the music catalog wire handlers: music_new, music_manifest, remove.

Each handler runs against a real ``MusicLibrary`` over a ``tmp_path`` root with a
fake ``WebSocket`` capturing frames. The reused invariants are asserted at these
new ops by name: a rejection sends an error frame AND leaves a WireReply WARNING
in the log; the album id is a catalog key (a non-hex/hostile id is refused by
``AlbumId``, never resolved as a path), so no authorized op deletes outside root.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast, final

from starlette.websockets import WebSocketDisconnect

from punt_vox.voxd.programs.catalog import Catalog
from punt_vox.voxd.programs.filesystem_store import FilesystemProgramStore
from punt_vox.voxd.programs.library import MusicLibrary
from punt_vox.voxd.programs.library_handlers import (
    MusicManifestHandler,
    MusicNewHandler,
    MusicRemoveHandler,
)
from punt_vox.voxd.programs.producer import PartSpec, ProducerBadInputError

from .conftest import QuietProducer, seed_album

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from starlette.websockets import WebSocket

    from punt_vox.voxd.programs.part import Part
    from punt_vox.voxd.programs.producer import Producer


def _capturing_ws() -> tuple[WebSocket, list[dict[str, object]]]:
    """Return a fake WebSocket and the list its ``send_json`` appends to."""
    sent: list[dict[str, object]] = []

    @final
    class _WS:
        async def send_json(self, payload: dict[str, object]) -> None:
            sent.append(payload)

    return cast("WebSocket", _WS()), sent


@final
class _BadPromptProducer:
    """A Producer that always rejects permanently."""

    __slots__ = ()

    async def produce(self, spec: PartSpec, target: Path) -> Part:
        del spec, target
        raise ProducerBadInputError("bad_prompt: refused")


def _library(root: Path, producer: Producer | None = None) -> MusicLibrary:
    store = FilesystemProgramStore(root)
    catalog = Catalog(store.scan())
    return MusicLibrary(catalog, store, root, producer or QuietProducer())


class TestMusicNewHandler:
    def test_success_acks_then_replies_bare_id(self, tmp_path: Path) -> None:
        library = _library(tmp_path / "programs")
        ws, sent = _capturing_ws()
        msg: dict[str, object] = {
            "type": "music_new",
            "id": "n1",
            "base_prompt": "pads",
        }
        asyncio.run(MusicNewHandler(library)(msg, ws))

        assert sent[0]["type"] == "generating"
        assert sent[-1]["type"] == "album"
        assert sent[-1]["parts"] == 1
        assert len(str(sent[-1]["album_id"])) == 6
        assert "path" not in sent[-1] and "host" not in sent[-1]

    def test_empty_prompt_rejected_before_ack(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        library = _library(tmp_path / "programs")
        ws, sent = _capturing_ws()
        msg: dict[str, object] = {"type": "music_new", "id": "n2", "base_prompt": ""}
        with caplog.at_level(logging.WARNING):
            asyncio.run(MusicNewHandler(library)(msg, ws))

        assert [f["type"] for f in sent] == ["error"]  # no 'generating' ack
        assert any("n2" in r.getMessage() for r in caplog.records)

    def test_whitespace_prompt_rejected_before_ack(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A blank (whitespace-only) prompt is refused pre-ack, like an empty one,
        rather than acking and failing later in generation."""
        library = _library(tmp_path / "programs")
        ws, sent = _capturing_ws()
        msg: dict[str, object] = {"type": "music_new", "id": "n5", "base_prompt": "   "}
        with caplog.at_level(logging.WARNING):
            asyncio.run(MusicNewHandler(library)(msg, ws))

        assert [f["type"] for f in sent] == ["error"]  # no 'generating' ack
        assert any("n5" in r.getMessage() for r in caplog.records)

    def test_non_string_prompt_is_a_clean_error(self, tmp_path: Path) -> None:
        """A non-string prompt yields an error before the ack; the parse must not
        escape the handler and tear the connection down."""
        library = _library(tmp_path / "programs")
        ws, sent = _capturing_ws()
        msg: dict[str, object] = {"type": "music_new", "id": "n4", "base_prompt": 123}
        asyncio.run(MusicNewHandler(library)(msg, ws))

        assert [f["type"] for f in sent] == ["error"]  # no 'generating' ack
        assert "must be a string" in str(sent[-1]["message"])

    def test_bad_prompt_acks_then_errors_and_audits(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        library = _library(tmp_path / "programs", _BadPromptProducer())
        ws, sent = _capturing_ws()
        msg: dict[str, object] = {"type": "music_new", "id": "n3", "base_prompt": "x"}
        with caplog.at_level(logging.WARNING):
            asyncio.run(MusicNewHandler(library)(msg, ws))

        assert sent[0]["type"] == "generating"
        assert sent[-1]["type"] == "error"
        assert any("n3" in r.getMessage() for r in caplog.records)

    def test_duplicate_name_rejected_before_ack(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A curated name already taken is refused BEFORE any 'generating' ack,
        joining the blank-prompt rejection ahead of the ack rather than acking and
        only failing once generation begins."""
        library = _library(tmp_path / "programs")
        ws0, _ = _capturing_ws()
        first: dict[str, object] = {
            "type": "music_new",
            "id": "d0",
            "base_prompt": "pads",
            "name": "dup",
        }
        asyncio.run(MusicNewHandler(library)(first, ws0))

        ws1, sent = _capturing_ws()
        second: dict[str, object] = {
            "type": "music_new",
            "id": "d1",
            "base_prompt": "more",
            "name": "dup",
        }
        with caplog.at_level(logging.WARNING):
            asyncio.run(MusicNewHandler(library)(second, ws1))

        assert [f["type"] for f in sent] == ["error"]  # no 'generating' ack precedes
        assert "already exists" in str(sent[-1]["message"])
        assert any("d1" in r.getMessage() for r in caplog.records)

    def test_non_string_name_rejected_before_ack(self, tmp_path: Path) -> None:
        """A non-string name is a malformed frame refused pre-ack, like a
        non-string prompt -- the name parse now runs before the ack, not after."""
        library = _library(tmp_path / "programs")
        ws, sent = _capturing_ws()
        msg: dict[str, object] = {
            "type": "music_new",
            "id": "n6",
            "base_prompt": "pads",
            "name": 123,
        }
        asyncio.run(MusicNewHandler(library)(msg, ws))

        assert [f["type"] for f in sent] == ["error"]  # no 'generating' ack
        assert "must be a string" in str(sent[-1]["message"])

    def test_duplicate_rejection_leaks_no_reservation(self, tmp_path: Path) -> None:
        """A pre-ack duplicate rejection holds nothing new, so authoring a distinct
        name straight after still acks and generates -- the rejected request left
        no phantom hold behind."""
        library = _library(tmp_path / "programs")
        ws0, _ = _capturing_ws()
        taken: dict[str, object] = {
            "type": "music_new",
            "id": "k0",
            "base_prompt": "pads",
            "name": "keep",
        }
        asyncio.run(MusicNewHandler(library)(taken, ws0))

        ws1, _ = _capturing_ws()
        dup: dict[str, object] = {
            "type": "music_new",
            "id": "k1",
            "base_prompt": "more",
            "name": "keep",
        }
        asyncio.run(MusicNewHandler(library)(dup, ws1))  # rejected pre-ack

        ws2, sent = _capturing_ws()
        other: dict[str, object] = {
            "type": "music_new",
            "id": "k2",
            "base_prompt": "more",
            "name": "other",
        }
        asyncio.run(MusicNewHandler(library)(other, ws2))
        assert sent[0]["type"] == "generating"
        assert sent[-1]["type"] == "album"

    def test_reservation_released_when_ack_undeliverable(self, tmp_path: Path) -> None:
        """If the peer vanishes before the ack lands, the reservation's context
        frees the held name so a retry is not falsely rejected as a duplicate."""
        library = _library(tmp_path / "programs")

        @final
        class _GoneOnAck:
            async def send_json(self, payload: dict[str, object]) -> None:
                if payload.get("type") == "generating":
                    raise WebSocketDisconnect(code=1000)

        gone = cast("WebSocket", _GoneOnAck())
        lost: dict[str, object] = {
            "type": "music_new",
            "id": "g1",
            "base_prompt": "pads",
            "name": "again",
        }
        asyncio.run(MusicNewHandler(library)(lost, gone))

        ws, sent = _capturing_ws()
        retry: dict[str, object] = {
            "type": "music_new",
            "id": "g2",
            "base_prompt": "pads",
            "name": "again",
        }
        asyncio.run(MusicNewHandler(library)(retry, ws))
        assert sent[0]["type"] == "generating"
        assert sent[-1]["type"] == "album"  # "again" was free, not a phantom dup


class TestMusicManifestHandler:
    def test_success_lists_parts(self, tmp_path: Path) -> None:
        root = tmp_path / "programs"
        seed_album(root, 1, 2, name="pads", album_id="a3f1c9")
        library = _library(root)
        ws, sent = _capturing_ws()
        msg: dict[str, object] = {"id": "m1", "album": "a3f1c9"}
        asyncio.run(MusicManifestHandler(library)(msg, ws))

        reply = sent[-1]
        assert reply["type"] == "manifest"
        assert str(reply["album"]).endswith("a3f1c9")
        parts = reply["parts"]
        assert isinstance(parts, list)
        assert {str(p["part"]) for p in parts} == {"001.mp3", "002.mp3"}

    def test_unknown_album_id_audited(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        library = _library(tmp_path / "programs")
        ws, sent = _capturing_ws()
        msg: dict[str, object] = {"id": "m2", "album": "abcdef"}
        with caplog.at_level(logging.WARNING):
            asyncio.run(MusicManifestHandler(library)(msg, ws))

        assert sent[-1]["type"] == "error"
        assert "no album named" in str(sent[-1]["message"])
        # A not-found album id is a client rejection (ValueError), not a fault.
        assert any(
            "rejected op" in r.getMessage() and "m2" in r.getMessage()
            for r in caplog.records
        )
        assert not any("operation failed" in r.getMessage() for r in caplog.records)


class TestMusicRemoveHandler:
    def test_removes_idle_album(self, tmp_path: Path) -> None:
        root = tmp_path / "programs"
        locator = seed_album(root, 1, name="idle", album_id="a3f1c9")
        library = _library(root)
        ws, sent = _capturing_ws()
        msg: dict[str, object] = {"type": "music_remove", "id": "r1", "album": "a3f1c9"}
        asyncio.run(MusicRemoveHandler(library, frozenset)(msg, ws))

        assert sent[-1] == {"type": "removed", "id": "r1", "album_id": "a3f1c9"}
        assert not (root / locator).exists()

    def test_refuses_playing_album(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        root = tmp_path / "programs"
        locator = seed_album(root, 1, name="live", album_id="a3f1c9")
        library = _library(root)
        ws, sent = _capturing_ws()
        msg: dict[str, object] = {"type": "music_remove", "id": "r2", "album": "a3f1c9"}
        with caplog.at_level(logging.WARNING):
            asyncio.run(
                MusicRemoveHandler(library, lambda: frozenset({locator}))(msg, ws)
            )

        assert sent[-1]["type"] == "error"
        assert "is playing" in str(sent[-1]["message"])
        assert (root / locator).is_dir()  # nothing deleted
        # The D-2 backing refusal is a client rejection (ValueError), not a fault.
        assert any(
            "rejected op" in r.getMessage() and "r2" in r.getMessage()
            for r in caplog.records
        )
        assert not any("operation failed" in r.getMessage() for r in caplog.records)

    def test_token_does_not_grant_fs_delete(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A hostile album ref is a bad catalog key, never a path -- nothing deleted."""
        root = tmp_path / "programs"
        outside = tmp_path / "victim.txt"
        outside.write_text("keep me")
        library = _library(root)
        ws, sent = _capturing_ws()
        for hostile in ("../../victim", "/etc/passwd", "a/b"):
            msg: dict[str, object] = {"type": "music_remove", "album": hostile}
            with caplog.at_level(logging.WARNING):
                asyncio.run(MusicRemoveHandler(library, frozenset)(msg, ws))
            assert sent[-1]["type"] == "error"
        assert outside.read_text() == "keep me"


class TestLibraryFaultClassification:
    """A LookupError/OSError from the library audits as a fault, not a rejection.

    The handlers split ``_LIBRARY_FAILURES`` via ``WireReply.reject_or_fault``: a
    deleted album dir (``LookupError``) or a filesystem fault (``OSError``) is a
    server-side operational failure, distinct from the ``ValueError`` rejections
    the other tests cover. Both still reply a clean id-stamped error frame rather
    than escaping to the router teardown.
    """

    def test_lookup_error_is_an_operational_fault(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        library = _library(tmp_path / "programs")

        def vanished(_self: MusicLibrary, _album_id: object) -> object:
            raise LookupError("album dir vanished mid-op")

        monkeypatch.setattr(MusicLibrary, "manifest", vanished)
        ws, sent = _capturing_ws()
        msg: dict[str, object] = {"id": "m9", "album": "a3f1c9"}
        with caplog.at_level(logging.WARNING, logger="punt_vox.voxd.wire_reply"):
            asyncio.run(MusicManifestHandler(library)(msg, ws))
        assert sent[-1]["type"] == "error"
        assert any(
            r.levelno == logging.ERROR and "operation failed" in r.getMessage()
            for r in caplog.records
        )
        assert not any("rejected op" in r.getMessage() for r in caplog.records)

    def test_corrupt_manifest_get_is_an_operational_fault(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A ``music get`` on an album whose on-disk manifest turned corrupt faults.

        The album is healthy when the catalog scans it, then its manifest is
        overwritten with non-UTF-8 bytes -- store corruption. ``music_manifest``
        re-reads it live, so ``_read_manifest_text`` raises ``OSError`` (not the
        ``ValueError`` a client rejection uses) and the handler audits it as an
        operational fault while still returning a clean id-stamped error frame.
        """
        root = tmp_path / "programs"
        locator = seed_album(root, 1, name="idle", album_id="a3f1c9")
        library = _library(root)  # the catalog scans the still-healthy album
        (root / locator / "manifest.json").write_bytes(b"\xff\xfe not utf-8")
        ws, sent = _capturing_ws()
        msg: dict[str, object] = {"id": "m8", "album": "a3f1c9"}
        with caplog.at_level(logging.WARNING, logger="punt_vox.voxd.wire_reply"):
            asyncio.run(MusicManifestHandler(library)(msg, ws))
        assert sent[-1]["type"] == "error"
        assert sent[-1]["id"] == "m8"  # a clean, id-stamped frame, not a torn socket
        assert any(
            r.levelno == logging.ERROR and "operation failed" in r.getMessage()
            for r in caplog.records
        )
        assert not any("rejected op" in r.getMessage() for r in caplog.records)

    def test_os_error_is_an_operational_fault(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        root = tmp_path / "programs"
        seed_album(root, 1, name="idle", album_id="a3f1c9")
        library = _library(root)

        def disk_fault(
            _self: MusicLibrary, _album_id: object, *, blocked: object
        ) -> object:
            del blocked
            raise OSError("disk failure removing album")

        monkeypatch.setattr(MusicLibrary, "remove", disk_fault)
        ws, sent = _capturing_ws()
        msg: dict[str, object] = {"type": "music_remove", "id": "r9", "album": "a3f1c9"}
        with caplog.at_level(logging.WARNING, logger="punt_vox.voxd.wire_reply"):
            asyncio.run(MusicRemoveHandler(library, frozenset)(msg, ws))
        assert sent[-1]["type"] == "error"
        assert any(
            r.levelno == logging.ERROR and "operation failed" in r.getMessage()
            for r in caplog.records
        )
        assert not any("rejected op" in r.getMessage() for r in caplog.records)
