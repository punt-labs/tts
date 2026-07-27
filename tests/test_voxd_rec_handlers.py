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
from typing import TYPE_CHECKING, cast, final

import pytest

from punt_vox.voxd.rec_handlers import RecListHandler, RecRemoveHandler
from punt_vox.voxd.record_store import RecordStore

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.websockets import WebSocket

_HOSTILE = {
    "absolute": "/etc/passwd",
    "traversal": "../../../etc/cron.d/x",
    "separator": "a/b.mp3",
    "empty": "",
    "nul": "bad\x00name.mp3",
    "nonprintable": "bad\nname.mp3",
}


def _capturing_ws() -> tuple[WebSocket, list[dict[str, object]]]:
    sent: list[dict[str, object]] = []

    @final
    class _WS:
        async def send_json(self, payload: dict[str, object]) -> None:
            sent.append(payload)

    return cast("WebSocket", _WS()), sent


def _store(tmp_path: Path) -> RecordStore:
    store = RecordStore(tmp_path / "recordings")
    store.root.mkdir(parents=True)
    return store


class TestRecList:
    def test_empty_store_lists_nothing(self, tmp_path: Path) -> None:
        ws, sent = _capturing_ws()
        asyncio.run(RecListHandler(_store(tmp_path))({"id": "l1"}, ws))
        assert sent[-1]["type"] == "recordings"
        assert sent[-1]["entries"] == []

    def test_lists_immediate_files_with_bytes(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        (store.root / "a.mp3").write_bytes(b"12345")
        (store.root / "b.mp3").write_bytes(b"12")
        ws, sent = _capturing_ws()
        asyncio.run(RecListHandler(store)({"id": "l1"}, ws))
        entries = sent[-1]["entries"]
        assert isinstance(entries, list)
        pairs = {(e["name"], e["bytes"]) for e in entries}
        assert pairs == {("a.mp3", 5), ("b.mp3", 2)}

    def test_listing_io_error_is_an_operational_fault(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An OSError enumerating the root is an operational fault, not a rejection.

        Per-entry stat faults are skipped in the store, but a fault reading the
        root itself is a server-side failure -- it must not escape to the router's
        broad except (which logs and drops the socket), and it is not a client
        rejection. The handler routes it through WireReply.fault: an id-stamped
        clean error frame audited at ERROR "operation failed", never WARNING
        "rejected op". Reaching the assertions without a raise is the connection-
        intact check.
        """
        store = _store(tmp_path)

        def boom(_self: RecordStore) -> object:
            raise OSError("permission denied on recordings root")

        monkeypatch.setattr(RecordStore, "entries", boom)
        ws, sent = _capturing_ws()
        with caplog.at_level(logging.WARNING, logger="punt_vox.voxd.wire_reply"):
            asyncio.run(RecListHandler(store)({"id": "l9"}, ws))
        assert sent[-1]["type"] == "error"
        assert sent[-1]["id"] == "l9"
        # OSError with no in-jail filename -> generic wire verdict, detail to log.
        assert sent[-1]["message"] == "operation failed"
        assert any(
            r.levelno == logging.ERROR
            and "operation failed" in r.getMessage()
            and "permission denied on recordings root" in r.getMessage()
            for r in caplog.records
        )
        assert not any("rejected op" in r.getMessage() for r in caplog.records)

    def test_does_not_recurse_into_subdirs(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        (store.root / "top.mp3").write_bytes(b"1")
        sub = store.root / "sub"
        sub.mkdir()
        (sub / "deep.mp3").write_bytes(b"1")
        ws, sent = _capturing_ws()
        asyncio.run(RecListHandler(store)({"id": "l1"}, ws))
        entries = sent[-1]["entries"]
        assert isinstance(entries, list)
        assert {e["name"] for e in entries} == {"top.mp3"}  # subdir not listed


class TestRecRemove:
    def test_removes_an_in_root_recording(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        (store.root / "gone.mp3").write_bytes(b"bytes")
        ws, sent = _capturing_ws()
        asyncio.run(RecRemoveHandler(store)({"id": "r1", "ref": "gone.mp3"}, ws))
        # The removed-id key is ``removed`` -- uniform with ``music remove`` and
        # the CLI/MCP readers, not the store-local ``name``.
        assert sent[-1] == {"type": "removed", "id": "r1", "removed": "gone.mp3"}
        assert not (store.root / "gone.mp3").exists()

    def test_not_found_is_a_client_rejection(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A well-formed ref that names no recording is a rejection, not a fault.

        ``remove`` raises ``FileNotFoundError`` ("names no recording"); the handler
        classifies it as a client rejection -- ``error`` (WARNING "rejected op") --
        matching how ``play``/``fetch`` answer a ref that names no recording, never
        the ERROR "operation failed" reserved for a daemon-side fault.
        """
        store = _store(tmp_path)
        ws, sent = _capturing_ws()
        with caplog.at_level(logging.WARNING, logger="punt_vox.voxd.wire_reply"):
            asyncio.run(RecRemoveHandler(store)({"id": "r1", "ref": "nope.mp3"}, ws))
        assert sent[-1]["type"] == "error"
        assert "no recording named" in str(sent[-1]["message"])
        assert any(
            "rejected op" in r.getMessage() and "r1" in r.getMessage()
            for r in caplog.records
        )
        assert not any("operation failed" in r.getMessage() for r in caplog.records)

    def test_oserror_on_unlink_is_an_operational_fault(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A PermissionError (an OSError) from the unlink is a fault, not a rejection.

        ``remove`` can raise an ``OSError`` beyond ``FileNotFoundError`` -- a denied
        unlink (``PermissionError``) or a device error. That is a server-side
        operational failure, distinct from the not-found rejection, so it routes
        through ``fault`` (ERROR "operation failed") never WARNING "rejected op",
        and replies an id-stamped frame instead of escaping to a router teardown
        (reaching the assertions without a raise is the connection-intact check).
        """
        store = _store(tmp_path)
        (store.root / "denied.mp3").write_bytes(b"bytes")

        def denied_remove(_self: RecordStore, _ref: str) -> None:
            raise PermissionError("[Errno 13] Permission denied")

        monkeypatch.setattr(RecordStore, "remove", denied_remove)
        ws, sent = _capturing_ws()
        with caplog.at_level(logging.WARNING, logger="punt_vox.voxd.wire_reply"):
            asyncio.run(RecRemoveHandler(store)({"id": "r7", "ref": "denied.mp3"}, ws))
        assert sent[-1]["type"] == "error"
        assert sent[-1]["id"] == "r7"
        # OSError with no in-jail filename -> generic wire verdict, detail to log.
        assert sent[-1]["message"] == "operation failed"
        assert any(
            r.levelno == logging.ERROR
            and "operation failed" in r.getMessage()
            and "Permission denied" in r.getMessage()
            for r in caplog.records
        )
        assert not any("rejected op" in r.getMessage() for r in caplog.records)

    def test_unlink_race_filenotfound_does_not_leak_absolute_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A TOCTOU unlink race is a fault whose wire frame carries no host path.

        The store's own "no recording named X" is a ``FileNotFoundError`` with
        ``filename`` ``None`` -- a client rejection sent verbatim. But if the entry
        vanishes between the stat and the ``unlink``, the OS raises a *raw*
        ``FileNotFoundError`` carrying ``filename=<absolute store path>``. That is
        a server-side fault, not a rejection, and its absolute prefix must never
        reach the client -- it routes through ``fault`` and crosses as a
        prefix-free message.
        """
        store = _store(tmp_path)
        (store.root / "racy.mp3").write_bytes(b"bytes")
        leaked = "/Users/someone/.punt-labs/vox/recordings/racy.mp3"

        def racing_remove(_self: RecordStore, _ref: str) -> None:
            raise FileNotFoundError(2, "No such file or directory", leaked)

        monkeypatch.setattr(RecordStore, "remove", racing_remove)
        ws, sent = _capturing_ws()
        with caplog.at_level(logging.WARNING, logger="punt_vox.voxd.wire_reply"):
            asyncio.run(RecRemoveHandler(store)({"id": "r8", "ref": "racy.mp3"}, ws))
        assert sent[-1]["type"] == "error"
        assert sent[-1]["id"] == "r8"
        # The wire frame carries no absolute prefix and no OS Errno.
        message = str(sent[-1]["message"])
        assert "/Users/" not in message
        assert not message.startswith("/")
        assert "Errno" not in message
        # It audits as a server fault, not a client rejection.
        assert any(
            r.levelno == logging.ERROR and "operation failed" in r.getMessage()
            for r in caplog.records
        )
        assert not any("rejected op" in r.getMessage() for r in caplog.records)

    def test_missing_ref_is_an_error(self, tmp_path: Path) -> None:
        ws, sent = _capturing_ws()
        asyncio.run(RecRemoveHandler(_store(tmp_path))({"id": "r1"}, ws))
        assert sent[-1]["type"] == "error"

    def test_non_string_ref_is_a_clean_error(self, tmp_path: Path) -> None:
        """A non-string ``ref`` yields an error frame; the parse must not escape
        the handler and tear the connection down."""
        store = _store(tmp_path)
        (store.root / "gone.mp3").write_bytes(b"bytes")
        ws, sent = _capturing_ws()
        asyncio.run(RecRemoveHandler(store)({"id": "r1", "ref": 123}, ws))
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
        with caplog.at_level(logging.WARNING, logger="punt_vox.voxd.wire_reply"):
            asyncio.run(RecRemoveHandler(store)({"id": "r9", "ref": ref}, ws))
        assert sent[-1]["type"] == "error"
        # A hostile ref is a client rejection: audited "rejected op", not "fault".
        assert any(
            "rejected op" in r.getMessage() and "r9" in r.getMessage()
            for r in caplog.records
        )
        assert not any("operation failed" in r.getMessage() for r in caplog.records)
        assert victim.read_text() == "keep me"  # nothing outside root deleted

    def test_token_does_not_grant_fs_delete(self, tmp_path: Path) -> None:
        """No authorized rec_remove, whatever its ref, deletes outside the root."""
        store = _store(tmp_path)
        victim = tmp_path / "secret.txt"
        victim.write_text("private")
        ws, _sent = _capturing_ws()
        for ref in _HOSTILE.values():
            asyncio.run(RecRemoveHandler(store)({"id": "t", "ref": ref}, ws))
        assert victim.read_text() == "private"
