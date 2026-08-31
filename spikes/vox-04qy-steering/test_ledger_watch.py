"""Pins for the ledger poller Arm 2's receipt checks block on."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from ledger_watch import LedgerWatch, prompt_of
from stamp import HookLedger, HookRecord


def _record(event: str, prompt: str, recv_seq: int) -> HookRecord:
    return HookRecord(
        recv_seq=recv_seq,
        session_seq=recv_seq,
        session_id="sess",
        event=event,
        received_at="2026-08-31T00:00:00+00:00",
        received_ns=1_780_000_000_000_000_000 + recv_seq,
        payload={"hook_event_name": event, "prompt": prompt},
    )


class TestWaitFor:
    """Blocking receipt checks over a growing JSONL file."""

    def test_finds_an_already_persisted_record(self, tmp_path: Path) -> None:
        ledger = HookLedger(tmp_path / "ledger.jsonl")
        ledger.append(_record("UserPromptSubmit", "hello MARKER-1", recv_seq=1))
        watch = LedgerWatch(ledger, poll_s=0.05)
        record = watch.wait_for(
            lambda r: "MARKER-1" in prompt_of(r), timeout_s=2, description="marker"
        )
        assert record.recv_seq == 1

    def test_sees_a_record_appended_while_waiting(self, tmp_path: Path) -> None:
        ledger = HookLedger(tmp_path / "ledger.jsonl")

        def late_append() -> None:
            time.sleep(0.2)
            ledger.append(_record("UserPromptSubmit", "late MARKER-2", recv_seq=1))

        writer = threading.Thread(target=late_append)
        writer.start()
        try:
            watch = LedgerWatch(ledger, poll_s=0.05)
            record = watch.wait_for(
                lambda r: "MARKER-2" in prompt_of(r),
                timeout_s=5,
                description="late marker",
            )
            assert "MARKER-2" in prompt_of(record)
        finally:
            writer.join()

    def test_timeout_names_the_awaited_condition(self, tmp_path: Path) -> None:
        watch = LedgerWatch(HookLedger(tmp_path / "ledger.jsonl"), poll_s=0.05)
        with pytest.raises(TimeoutError, match="ghost record"):
            watch.wait_for(lambda r: False, timeout_s=0.2, description="ghost record")

    def test_on_tick_runs_each_poll(self, tmp_path: Path) -> None:
        ticks: list[int] = []
        watch = LedgerWatch(
            HookLedger(tmp_path / "ledger.jsonl"),
            poll_s=0.05,
            on_tick=lambda: ticks.append(1),
        )
        with pytest.raises(TimeoutError):
            watch.wait_for(lambda r: False, timeout_s=0.2, description="none")
        assert ticks


class TestWaitUntil:
    """Ledger-level conditions (event counts) block the same way."""

    def test_holds_once_the_count_is_reached(self, tmp_path: Path) -> None:
        ledger = HookLedger(tmp_path / "ledger.jsonl")
        ledger.append(_record("Stop", "", recv_seq=1))
        ledger.append(_record("Stop", "", recv_seq=2))
        watch = LedgerWatch(ledger, poll_s=0.05)
        watch.wait_until(
            lambda: watch.count("Stop") >= 2, timeout_s=2, description="two stops"
        )

    def test_deadline_names_the_condition(self, tmp_path: Path) -> None:
        watch = LedgerWatch(HookLedger(tmp_path / "ledger.jsonl"), poll_s=0.05)
        with pytest.raises(TimeoutError, match="three stops"):
            watch.wait_until(lambda: False, timeout_s=0.2, description="three stops")


class TestCountAndPrompt:
    """The counters the sequencing logic keys on."""

    def test_count_by_event_type(self, tmp_path: Path) -> None:
        ledger = HookLedger(tmp_path / "ledger.jsonl")
        ledger.append(_record("Stop", "", recv_seq=1))
        ledger.append(_record("UserPromptSubmit", "x", recv_seq=2))
        ledger.append(_record("Stop", "", recv_seq=3))
        watch = LedgerWatch(ledger, poll_s=0.05)
        assert watch.count("Stop") == 2
        assert watch.count("UserPromptSubmit") == 1

    def test_prompt_of_missing_field_is_empty(self, tmp_path: Path) -> None:
        record = _record("Stop", "", recv_seq=1)
        stripped = HookRecord(
            recv_seq=record.recv_seq,
            session_seq=record.session_seq,
            session_id=record.session_id,
            event=record.event,
            received_at=record.received_at,
            received_ns=record.received_ns,
            payload={"hook_event_name": "Stop"},
        )
        assert prompt_of(stripped) == ""
