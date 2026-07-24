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
        msg: dict[str, object] = {"type": "music_new", "id": "n1", "prompt": "pads"}
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
        msg: dict[str, object] = {"type": "music_new", "id": "n2", "prompt": ""}
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
        msg: dict[str, object] = {"type": "music_new", "id": "n5", "prompt": "   "}
        with caplog.at_level(logging.WARNING):
            asyncio.run(MusicNewHandler(library)(msg, ws))

        assert [f["type"] for f in sent] == ["error"]  # no 'generating' ack
        assert any("n5" in r.getMessage() for r in caplog.records)

    def test_non_string_prompt_is_a_clean_error(self, tmp_path: Path) -> None:
        """A non-string prompt yields an error before the ack; the parse must not
        escape the handler and tear the connection down."""
        library = _library(tmp_path / "programs")
        ws, sent = _capturing_ws()
        msg: dict[str, object] = {"type": "music_new", "id": "n4", "prompt": 123}
        asyncio.run(MusicNewHandler(library)(msg, ws))

        assert [f["type"] for f in sent] == ["error"]  # no 'generating' ack
        assert "must be a string" in str(sent[-1]["message"])

    def test_bad_prompt_acks_then_errors_and_audits(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        library = _library(tmp_path / "programs", _BadPromptProducer())
        ws, sent = _capturing_ws()
        msg: dict[str, object] = {"type": "music_new", "id": "n3", "prompt": "x"}
        with caplog.at_level(logging.WARNING):
            asyncio.run(MusicNewHandler(library)(msg, ws))

        assert sent[0]["type"] == "generating"
        assert sent[-1]["type"] == "error"
        assert any("n3" in r.getMessage() for r in caplog.records)


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
        assert any("m2" in r.getMessage() for r in caplog.records)


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
        assert any("r2" in r.getMessage() for r in caplog.records)

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
