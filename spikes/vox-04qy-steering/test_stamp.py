"""Pins for the copied vox-73y7 ledger core this spike relies on.

The frozen vox-juhw spike already proves ordering, attribution, and
durability; those tests are not repeated here. This file pins what this
spike's copy must keep working: the ``received_ns`` receipt stamp (the
store side of the send-to-hook-visible latency), the ``relay_start_ns``
/ ``relay_seq`` payload accessors, and the credential redaction
end-to-end through a ledger file on disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stamp import HookLedger, HookRecord, SequenceStamper


def _stamped(payload: dict[str, object]) -> HookRecord:
    return SequenceStamper().stamp("PostToolUse", payload)


class TestReceivedNs:
    """The numeric receipt stamp latency computation pairs against."""

    def test_received_ns_is_a_positive_wall_clock_reading(self) -> None:
        record = _stamped({"session_id": "a"})
        # time.time_ns() readings are ~1.7e18 in this era; anything tiny
        # would mean a monotonic-clock mixup, which cannot pair with the
        # sender's time.time_ns() stamp.
        assert record.received_ns > 10**18

    def test_received_ns_never_decreases_across_stamps(self) -> None:
        stamper = SequenceStamper()
        readings = [
            stamper.stamp("Stop", {"session_id": "a"}).received_ns for _ in range(5)
        ]
        assert readings == sorted(readings)

    def test_received_ns_roundtrips_through_jsonl(self) -> None:
        record = _stamped({"session_id": "a"})
        assert HookRecord.from_json(record.to_json()) == record

    def test_line_without_received_ns_is_rejected(self) -> None:
        # A juhw-era ledger line has no received_ns; this spike reads only
        # its own ledgers, so the missing field is a hard error, not a
        # default (no compat shim).
        line = (
            '{"event": "Stop", "payload": {}, "received_at": "t",'
            ' "recv_seq": 1, "session_id": "a", "session_seq": 1}'
        )
        with pytest.raises(ValueError, match="received_ns"):
            HookRecord.from_json(line)


class TestRelayAccessors:
    """Sender-side stamps: present -> int, absent or wrong-shaped -> None."""

    def test_relay_start_ns_returns_the_sender_stamp(self) -> None:
        record = _stamped(
            {"session_id": "a", "relay_start_ns": 1_700_000_000_000_000_000}
        )
        assert record.relay_start_ns() == 1_700_000_000_000_000_000

    def test_relay_seq_returns_the_sender_sequence(self) -> None:
        record = _stamped({"session_id": "a", "relay_seq": 17})
        assert record.relay_seq() == 17

    @pytest.mark.parametrize(
        "payload", [{}, {"relay_start_ns": "123"}, {"relay_start_ns": 1.5}]
    )
    def test_relay_start_ns_is_none_for_unstamped_payloads(
        self, payload: dict[str, object]
    ) -> None:
        assert _stamped({"session_id": "a", **payload}).relay_start_ns() is None

    @pytest.mark.parametrize("payload", [{}, {"relay_seq": "7"}, {"relay_seq": None}])
    def test_relay_seq_is_none_for_unstamped_payloads(
        self, payload: dict[str, object]
    ) -> None:
        assert _stamped({"session_id": "a", **payload}).relay_seq() is None

    def test_relay_stamps_survive_redaction_pass(self) -> None:
        # The stamper rewrites the payload (redaction + scrubbing); the
        # relay stamps must come out the other side intact or latency and
        # gap detection are blind.
        record = _stamped(
            {"session_id": "a", "relay_start_ns": 42, "relay_seq": 3, "api_key": "sk-x"}
        )
        assert record.relay_start_ns() == 42
        assert record.relay_seq() == 3


class TestRedactionEndToEnd:
    """Credential-shaped values never reach the ledger file on disk."""

    def test_nested_credentials_are_absent_from_the_ledger_bytes(
        self, tmp_path: Path
    ) -> None:
        ledger = HookLedger(tmp_path / "ledger.jsonl")
        record = SequenceStamper().stamp(
            "PostToolUse",
            {
                "session_id": "a",
                "tool_input": {
                    "headers": {"Api_Key": "sk-live-abc123"},
                    "signed_url": "wss://host?sig=SECRETSIG",
                    "body": ["ok", {"refresh_token": "tok-refresh-999"}],
                },
                "persistent_session_token": "tok-top-level",
            },
        )
        ledger.append(record)
        raw = ledger.path.read_text(encoding="utf-8")
        secrets = ("sk-live-abc123", "SECRETSIG", "tok-refresh-999", "tok-top-level")
        for secret in secrets:
            assert secret not in raw
        assert "[redacted]" in raw
        # And the roundtrip still parses -- redaction never corrupts shape.
        (reread,) = ledger.records()
        assert reread.session_id == "a"

    def test_snapshot_read_skips_only_the_torn_tail(self, tmp_path: Path) -> None:
        ledger = HookLedger(tmp_path / "ledger.jsonl")
        stamper = SequenceStamper()
        ledger.append(stamper.stamp("SessionStart", {"session_id": "a"}))
        ledger.append(stamper.stamp("Stop", {"session_id": "a"}))
        with ledger.path.open("a", encoding="utf-8") as handle:
            handle.write('{"recv_seq": 3, "sess')  # mid-append fragment
        assert len(ledger.records_snapshot()) == 2
        # Strict read treats the fragment as corruption. The concrete error
        # is json.JSONDecodeError, a ValueError subclass.
        with pytest.raises(ValueError):
            ledger.records()
