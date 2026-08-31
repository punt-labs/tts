"""Pins for the sender-side stamp: the counter gap detection trusts.

Gap detection is only as good as the sender counter: if ``SessionCounter``
ever skips or repeats under concurrent hook fires, the verdict reads a
phantom gap (or misses a real one). These tests prove monotonicity,
per-session isolation, and atomicity offline, plus the stdin->stdout
stamping contract of ``main``.
"""

from __future__ import annotations

import io
import json
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import relay_stamp
from relay_stamp import SessionCounter

if TYPE_CHECKING:
    import pytest


class TestSessionCounter:
    """Per-session monotonic counter, atomic across concurrent fires."""

    def test_counts_from_one_and_increments(self, tmp_path: Path) -> None:
        counter = SessionCounter(tmp_path, "sess-a")
        assert [counter.next(), counter.next(), counter.next()] == [1, 2, 3]

    def test_sessions_do_not_share_a_counter(self, tmp_path: Path) -> None:
        a = SessionCounter(tmp_path, "sess-a")
        b = SessionCounter(tmp_path, "sess-b")
        assert a.next() == 1
        assert a.next() == 2
        assert b.next() == 1

    def test_counter_survives_a_fresh_instance(self, tmp_path: Path) -> None:
        # Hook commands are separate processes; each builds a fresh
        # SessionCounter over the same file and must continue, not restart.
        assert SessionCounter(tmp_path, "sess-a").next() == 1
        assert SessionCounter(tmp_path, "sess-a").next() == 2

    def test_empty_session_id_uses_the_unattributed_file(self, tmp_path: Path) -> None:
        SessionCounter(tmp_path, "").next()
        assert (tmp_path / "unattributed.seq").exists()

    def test_hostile_session_id_cannot_escape_the_counter_dir(
        self, tmp_path: Path
    ) -> None:
        SessionCounter(tmp_path, "../../etc/passwd").next()
        written = list(tmp_path.rglob("*.seq"))
        assert len(written) == 1
        assert written[0].parent == tmp_path

    def test_next_fsyncs_the_flushed_value_before_returning(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Durability pin: a kill right after next() returns must not roll
        # the file back and reuse a relay_seq. At the moment of fsync the
        # file must already hold the new value -- flush precedes sync,
        # and the sync happens before the value is handed out.
        real_fsync = os.fsync
        synced: list[str] = []

        def spy(fd: int) -> None:
            real_fsync(fd)
            synced.append((tmp_path / "sess-a.seq").read_text(encoding="utf-8"))

        monkeypatch.setattr("relay_stamp.os.fsync", spy)
        assert SessionCounter(tmp_path, "sess-a").next() == 1
        assert SessionCounter(tmp_path, "sess-a").next() == 2  # simulated reopen
        assert synced == ["1", "2"]

    def test_concurrent_increments_never_skip_or_repeat(self, tmp_path: Path) -> None:
        # A Stop hook can fire while a PostToolUse relay is still running;
        # flock must serialize them. 8 threads x 25 increments must yield
        # exactly 1..200 with no duplicates.
        results: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            counter = SessionCounter(tmp_path, "sess-a")
            for _ in range(25):
                value = counter.next()
                with lock:
                    results.append(value)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(results) == list(range(1, 201))


class TestMain:
    """The stdin -> stamped stdout contract of the relay wrapper."""

    def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        payload: object,
        extra_argv: list[str] | None = None,
    ) -> object:
        argv = ["relay_stamp.py", "--counter-dir", str(tmp_path)]
        argv.extend(extra_argv or [])
        monkeypatch.setattr("sys.argv", argv)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        out = io.StringIO()
        monkeypatch.setattr("sys.stdout", out)
        relay_stamp.main()
        return json.loads(out.getvalue())

    def test_stamps_relay_seq_and_start_ns(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        stamped = self._run(
            monkeypatch, tmp_path, {"session_id": "s", "hook_event_name": "Stop"}
        )
        assert isinstance(stamped, dict)
        assert stamped["relay_seq"] == 1
        assert isinstance(stamped["relay_start_ns"], int)
        assert stamped["relay_start_ns"] > 10**18
        assert stamped["hook_event_name"] == "Stop"  # original fields intact

    def test_shell_start_ns_wins_over_interpreter_start(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        stamped = self._run(
            monkeypatch,
            tmp_path,
            {"session_id": "s"},
            extra_argv=["--start-ns", "1234567890123456789"],
        )
        assert isinstance(stamped, dict)
        assert stamped["relay_start_ns"] == 1234567890123456789

    def test_start_ns_zero_is_not_treated_as_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A falsy-but-present shell timestamp must win over the
        # interpreter's own start; only an ABSENT flag falls back.
        stamped = self._run(
            monkeypatch, tmp_path, {"session_id": "s"}, extra_argv=["--start-ns", "0"]
        )
        assert isinstance(stamped, dict)
        assert stamped["relay_start_ns"] == 0

    def test_consecutive_fires_advance_the_sequence(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        first = self._run(monkeypatch, tmp_path, {"session_id": "s"})
        second = self._run(monkeypatch, tmp_path, {"session_id": "s"})
        assert isinstance(first, dict)
        assert isinstance(second, dict)
        assert (first["relay_seq"], second["relay_seq"]) == (1, 2)

    def test_non_object_payload_is_forwarded_untouched(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        assert self._run(monkeypatch, tmp_path, ["not", "a", "dict"]) == [
            "not",
            "a",
            "dict",
        ]
        assert list(tmp_path.glob("*.seq")) == []  # no counter burned
