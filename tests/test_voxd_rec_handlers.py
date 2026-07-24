"""Tests for punt_vox.voxd.rec_handlers -- rec_list and rec_remove.

Each handler runs against a real ``RecordStore`` under ``tmp_path`` with a fake
WebSocket capturing frames. rec_list enumerates only the immediate in-root files;
rec_remove reuses the shared containment validator, so the corpus of hostile refs
is re-asserted here -- each rejection yields an error frame AND a WireReply WARNING
audit line, and nothing outside the root is deleted (the token-does-not-grant-
fs-delete trust-model twin).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, final

import pytest

from punt_vox.voxd.rec_handlers import RecListHandler, RecRemoveHandler
from punt_vox.voxd.record_store import RecordStore

if TYPE_CHECKING:
    from pathlib import Path

_HOSTILE = {
    "absolute": "/etc/passwd",
    "traversal": "../../../etc/cron.d/x",
    "separator": "a/b.mp3",
    "empty": "",
    "nul": "bad\x00name.mp3",
    "nonprintable": "bad\nname.mp3",
}


def _capturing_ws() -> tuple[object, list[dict[str, object]]]:
    sent: list[dict[str, object]] = []

    @final
    class _WS:
        async def send_json(self, payload: dict[str, object]) -> None:
            sent.append(payload)

    return _WS(), sent


def _store(tmp_path: Path) -> RecordStore:
    store = RecordStore(tmp_path / "recordings")
    store.root.mkdir(parents=True)
    return store


class TestRecList:
    def test_empty_store_lists_nothing(self, tmp_path: Path) -> None:
        ws, sent = _capturing_ws()
        asyncio.run(RecListHandler(_store(tmp_path))({"id": "l1"}, ws))  # type: ignore[arg-type]
        assert sent[-1]["type"] == "recordings"
        assert sent[-1]["entries"] == []

    def test_lists_immediate_files_with_bytes(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        (store.root / "a.mp3").write_bytes(b"12345")
        (store.root / "b.mp3").write_bytes(b"12")
        ws, sent = _capturing_ws()
        asyncio.run(RecListHandler(store)({"id": "l1"}, ws))  # type: ignore[arg-type]

        entries = sent[-1]["entries"]
        assert isinstance(entries, list)
        pairs = {(e["name"], e["bytes"]) for e in entries}
        assert pairs == {("a.mp3", 5), ("b.mp3", 2)}

    def test_listing_io_error_is_a_clean_error_frame(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An OSError enumerating the root is an id-stamped error frame, not a teardown.

        Per-entry stat faults are skipped in the store, but a fault reading the
        root itself must not escape to the router's broad except (which logs and
        drops the socket). The handler catches it and replies cleanly -- matching
        RecRemoveHandler -- and this test reaching its assertions (no raise out of
        the call) is itself the "connection intact" check.
        """
        store = _store(tmp_path)

        def boom(_self: RecordStore) -> object:
            raise OSError("permission denied on recordings root")

        monkeypatch.setattr(RecordStore, "entries", boom)
        ws, sent = _capturing_ws()
        asyncio.run(RecListHandler(store)({"id": "l9"}, ws))  # type: ignore[arg-type]

        assert sent[-1]["type"] == "error"
        assert sent[-1]["id"] == "l9"
        assert "permission denied" in str(sent[-1]["message"])

    def test_does_not_recurse_into_subdirs(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        (store.root / "top.mp3").write_bytes(b"1")
        sub = store.root / "sub"
        sub.mkdir()
        (sub / "deep.mp3").write_bytes(b"1")
        ws, sent = _capturing_ws()
        asyncio.run(RecListHandler(store)({"id": "l1"}, ws))  # type: ignore[arg-type]

        entries = sent[-1]["entries"]
        assert isinstance(entries, list)
        assert {e["name"] for e in entries} == {"top.mp3"}  # subdir not listed


class TestRecRemove:
    def test_removes_an_in_root_recording(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        (store.root / "gone.mp3").write_bytes(b"bytes")
        ws, sent = _capturing_ws()
        asyncio.run(RecRemoveHandler(store)({"id": "r1", "ref": "gone.mp3"}, ws))  # type: ignore[arg-type]

        assert sent[-1] == {"type": "removed", "id": "r1", "name": "gone.mp3"}
        assert not (store.root / "gone.mp3").exists()

    def test_not_found_is_an_error(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        ws, sent = _capturing_ws()
        asyncio.run(RecRemoveHandler(store)({"id": "r1", "ref": "nope.mp3"}, ws))  # type: ignore[arg-type]

        assert sent[-1]["type"] == "error"
        assert "no recording named" in str(sent[-1]["message"])

    def test_missing_ref_is_an_error(self, tmp_path: Path) -> None:
        ws, sent = _capturing_ws()
        asyncio.run(RecRemoveHandler(_store(tmp_path))({"id": "r1"}, ws))  # type: ignore[arg-type]
        assert sent[-1]["type"] == "error"

    def test_non_string_ref_is_a_clean_error(self, tmp_path: Path) -> None:
        """A non-string ``ref`` yields an error frame; the parse must not escape
        the handler and tear the connection down."""
        store = _store(tmp_path)
        (store.root / "gone.mp3").write_bytes(b"bytes")
        ws, sent = _capturing_ws()
        asyncio.run(RecRemoveHandler(store)({"id": "r1", "ref": 123}, ws))  # type: ignore[arg-type]

        assert sent[-1]["type"] == "error"
        assert "must be a string" in str(sent[-1]["message"])
        assert (store.root / "gone.mp3").exists()  # nothing removed

    @pytest.mark.parametrize(("kind", "ref"), sorted(_HOSTILE.items()))
    def test_hostile_ref_rejected_and_audited(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, kind: str, ref: str
    ) -> None:
        """Each rejection class: an error frame, a WARNING audit line, no deletion.

        Named per class so a failure points at the exact corpus member:
        rec_remove_absolute / _traversal / _separator / _empty / _nul /
        _nonprintable.
        """
        del kind
        store = _store(tmp_path)
        victim = tmp_path / "victim.txt"
        victim.write_text("keep me")
        ws, sent = _capturing_ws()
        with caplog.at_level(logging.WARNING):
            asyncio.run(RecRemoveHandler(store)({"id": "r9", "ref": ref}, ws))  # type: ignore[arg-type]

        assert sent[-1]["type"] == "error"
        assert any("r9" in r.getMessage() for r in caplog.records)
        assert victim.read_text() == "keep me"  # nothing outside root deleted

    def test_token_does_not_grant_fs_delete(self, tmp_path: Path) -> None:
        """No authorized rec_remove, whatever its ref, deletes outside the root."""
        store = _store(tmp_path)
        victim = tmp_path / "secret.txt"
        victim.write_text("private")
        ws, _sent = _capturing_ws()
        for ref in _HOSTILE.values():
            asyncio.run(RecRemoveHandler(store)({"id": "t", "ref": ref}, ws))  # type: ignore[arg-type]
        assert victim.read_text() == "private"
