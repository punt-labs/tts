"""The stub store speaks mcp-proxy's wire contract and stamps durably.

`HookStore.process` is the seam: every wire behavior (sync request, async
notification, health, parse error, unknown method) is tested here without a
socket or a spawned claude session.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hook_store import HookStore
from stamp import HookLedger

if TYPE_CHECKING:
    from pathlib import Path


def _store(tmp_path: Path) -> tuple[HookStore, HookLedger]:
    ledger = HookLedger(tmp_path / "ledger.jsonl")
    return HookStore(ledger), ledger


def _sync_hook(event: str, session: str, rpc_id: int = 1) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": f"hook/{event}",
            "params": {"session_id": session, "hook_event_name": event},
        }
    )


class TestSyncHookRelay:
    """Sync requests -- the default `mcp-proxy --hook <Event>` shape."""

    def test_hook_request_is_stamped_persisted_and_acknowledged(
        self, tmp_path: Path
    ) -> None:
        store, ledger = _store(tmp_path)
        response = store.process(_sync_hook("SessionStart", "sess-1"))
        assert response is not None
        parsed = json.loads(response)
        assert parsed["id"] == 1
        assert parsed["result"]["ok"] is True
        assert parsed["result"]["recv_seq"] == 1
        records = ledger.records()
        assert len(records) == 1
        assert records[0].event == "SessionStart"
        assert records[0].session_id == "sess-1"

    def test_persistence_precedes_acknowledgement(self, tmp_path: Path) -> None:
        store, ledger = _store(tmp_path)
        store.process(_sync_hook("SessionStart", "s"))
        # The record is on disk by the time the response exists; a fresh
        # reader (simulating a post-SIGKILL inspection) sees it.
        assert len(HookLedger(ledger.path).records()) == 1

    def test_interleaved_sessions_keep_both_orders(self, tmp_path: Path) -> None:
        store, ledger = _store(tmp_path)
        for event, session in (
            ("SessionStart", "a"),
            ("SessionStart", "b"),
            ("PostToolUse", "a"),
            ("Stop", "b"),
            ("Stop", "a"),
        ):
            store.process(_sync_hook(event, session))
        records = ledger.records()
        assert [r.recv_seq for r in records] == [1, 2, 3, 4, 5]
        a_seqs = [r.session_seq for r in records if r.session_id == "a"]
        b_seqs = [r.session_seq for r in records if r.session_id == "b"]
        assert a_seqs == [1, 2, 3]
        assert b_seqs == [1, 2]


class TestNotificationRelay:
    """Async notifications (`--hook --async`) get no response but persist."""

    def test_notification_persists_without_response(self, tmp_path: Path) -> None:
        store, ledger = _store(tmp_path)
        frame = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "hook/SessionEnd",
                "params": {"session_id": "s"},
            }
        )
        assert store.process(frame) is None
        assert ledger.records()[0].event == "SessionEnd"


class TestStoreEdges:
    """Health, parse errors, and unknown methods."""

    def test_health_reports_record_count(self, tmp_path: Path) -> None:
        store, _ = _store(tmp_path)
        store.process(_sync_hook("Stop", "s"))
        response = store.process(
            json.dumps({"jsonrpc": "2.0", "id": 9, "method": "store/health"})
        )
        assert response is not None
        assert json.loads(response)["result"] == {"status": "ok", "records": 1}

    def test_invalid_json_yields_parse_error(self, tmp_path: Path) -> None:
        store, ledger = _store(tmp_path)
        response = store.process("{not json")
        assert response is not None
        assert json.loads(response)["error"]["code"] == -32700
        assert ledger.records() == ()

    def test_frame_without_method_yields_parse_error(self, tmp_path: Path) -> None:
        store, _ = _store(tmp_path)
        response = store.process(json.dumps({"jsonrpc": "2.0", "id": 1}))
        assert response is not None
        assert json.loads(response)["error"]["code"] == -32700

    def test_unknown_method_request_yields_method_not_found(
        self, tmp_path: Path
    ) -> None:
        store, _ = _store(tmp_path)
        response = store.process(
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call"})
        )
        assert response is not None
        assert json.loads(response)["error"]["code"] == -32601

    def test_unknown_method_notification_is_dropped(self, tmp_path: Path) -> None:
        store, ledger = _store(tmp_path)
        response = store.process(json.dumps({"jsonrpc": "2.0", "method": "noise"}))
        assert response is None
        assert ledger.records() == ()

    def test_hook_with_null_params_still_lands(self, tmp_path: Path) -> None:
        store, ledger = _store(tmp_path)
        frame = json.dumps(
            {"jsonrpc": "2.0", "id": 3, "method": "hook/Stop", "params": None}
        )
        response = store.process(frame)
        assert response is not None
        assert json.loads(response)["result"]["ok"] is True
        assert ledger.records()[0].session_id == "unattributed"
