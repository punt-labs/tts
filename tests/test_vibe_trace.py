"""Tests for the durable vibe-trace sink (src/punt_vox/vibe_trace.py)."""

from __future__ import annotations

import multiprocessing
import os
import re
from collections.abc import Buffer
from pathlib import Path
from typing import TYPE_CHECKING

from punt_vox import paths
from punt_vox.vibe_trace import VibeTraceLog

if TYPE_CHECKING:
    import pytest

_LINE = re.compile(r"^\[vibe-trace\] proc=\d+ seq=\d+ [x]{40}$")


def _append_many(path_str: str, proc_id: int, count: int) -> None:
    """Append *count* fixed-shape trace lines -- a spawn-safe multiprocess worker.

    Each line is a single ``proc``/``seq`` record padded to a stable width, so a
    torn or interleaved write shows up as a line that fails :data:`_LINE`.
    """
    trace = VibeTraceLog(Path(path_str))
    for seq in range(count):
        trace.record(f"proc={proc_id} seq={seq} {'x' * 40}")


class TestVibeTraceLog:
    """The sink appends greppable ``[vibe-trace]`` lines to a durable file."""

    def test_default_path_lives_under_state_logs(self) -> None:
        assert VibeTraceLog.default().path.name == "vibe-trace.log"
        assert VibeTraceLog.default().path.parent.name == "logs"

    def test_record_appends_prefixed_line(self, tmp_path: Path) -> None:
        trace = VibeTraceLog(tmp_path / "vibe-trace.log")
        trace.record("vibe set mood=relaxing music_playing=true hint_emitted=true")
        assert trace.path.read_text(encoding="utf-8") == (
            "[vibe-trace] vibe set mood=relaxing music_playing=true hint_emitted=true\n"
        )

    def test_record_appends_without_truncating(self, tmp_path: Path) -> None:
        trace = VibeTraceLog(tmp_path / "vibe-trace.log")
        trace.record("nudge fired counter=5->0 mode=auto")
        trace.record("music off")
        lines = trace.path.read_text(encoding="utf-8").splitlines()
        assert lines == [
            "[vibe-trace] nudge fired counter=5->0 mode=auto",
            "[vibe-trace] music off",
        ]

    def test_record_creates_missing_parent_dir(self, tmp_path: Path) -> None:
        trace = VibeTraceLog(tmp_path / "logs" / "nested" / "vibe-trace.log")
        trace.record("music off")
        assert trace.path.is_file()

    def test_record_file_is_private(self, tmp_path: Path) -> None:
        trace = VibeTraceLog(tmp_path / "vibe-trace.log")
        trace.record("music off")
        assert (trace.path.stat().st_mode & 0o077) == 0

    def test_record_creates_private_parent_dir(self, tmp_path: Path) -> None:
        """A parent dir the sink creates is private -- no group/other access."""
        logs = tmp_path / "logs"
        VibeTraceLog(logs / "vibe-trace.log").record("music off")
        assert (logs.stat().st_mode & 0o077) == 0

    def test_record_tightens_loose_parent_dir(self, tmp_path: Path) -> None:
        """A pre-created group/other-readable dir is tightened to 0o700 on append.

        The hook may create ``logs/`` first under a permissive umask, landing it
        at ~0o755 while the file is forced 0o600. ``record`` must ``chmod`` the
        dir private regardless of who created it, or the proof trail is listable.
        """
        logs = tmp_path / "logs"
        logs.mkdir()
        logs.chmod(0o755)  # simulate a loose create by the peer process
        VibeTraceLog(logs / "vibe-trace.log").record("music off")
        assert (logs.stat().st_mode & 0o077) == 0

    def test_record_appends_when_dir_chmod_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A writable dir we can't ``chmod`` still accepts the trace -- no silent drop.

        The 0o700 tighten is best-effort hardening, not a precondition for the
        append. In a shared or wrong-ownership setup we can append to ``logs/``
        but not ``chmod`` it; if the tighten were in the write path, its
        ``PermissionError`` would drop the trace while ``is_writable`` (which
        probes access, not chmod) still reported healthy -- status diverging
        from reality. The append must succeed and match ``is_writable``.
        """
        logs = tmp_path / "logs"
        logs.mkdir()
        trace = VibeTraceLog(logs / "vibe-trace.log")

        def deny_chmod(_self: object, _mode: int) -> None:
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr("punt_vox.private_state.Path.chmod", deny_chmod)

        trace.record("music off")

        assert trace.is_writable() is True  # status reports healthy...
        assert trace.path.read_text(encoding="utf-8") == "[vibe-trace] music off\n"

    def test_record_surfaces_short_write_as_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A short write is attempted exactly once, never looped into a torn append.

        Atomicity beats completeness: re-issuing the remainder would be a second
        ``O_APPEND`` a concurrent writer could split. The composed sink writes once
        and swallows the short count to ``sys.__stderr__`` (never through logging,
        which would recurse into the client log handler).
        """
        real_write = os.write
        calls = {"n": 0}

        def always_short(fd: int, data: Buffer) -> int:
            calls["n"] += 1
            return real_write(fd, bytes(data)[:5])  # every write is short

        monkeypatch.setattr("punt_vox.append_log.os.write", always_short)
        VibeTraceLog(tmp_path / "vibe-trace.log").record("music off")

        assert calls["n"] == 1  # one atomic append -- no remainder loop

    def test_record_sanitizes_control_chars_to_one_line(self, tmp_path: Path) -> None:
        """A style/name smuggling ``\\n``/``\\r``/a control char stays one line.

        Tag canonicalisation only trims the ends, so an MCP-controlled value can
        carry an embedded newline (which would forge a second ``[vibe-trace]``
        line) or a raw control byte (which would corrupt a terminal on ``cat``).
        ``record`` escapes them, so the appended record is exactly one physical
        line with no C0 control byte on disk.
        """
        trace = VibeTraceLog(tmp_path / "vibe-trace.log")
        trace.record("music on style=jazz\n[vibe-trace] forged\rX\x07")

        raw = trace.path.read_bytes()
        assert raw.count(b"\n") == 1  # only the record terminator -- no forged line
        assert raw.endswith(b"\n")
        assert not any(b < 0x20 or b == 0x7F for b in raw[:-1])  # no raw control byte
        assert trace.path.read_text(encoding="utf-8") == (
            "[vibe-trace] music on style=jazz\\n[vibe-trace] forged\\rX\\x07\n"
        )

    def test_record_escapes_unicode_line_separators(self, tmp_path: Path) -> None:
        """NEL / LINE / PARAGRAPH separators can't forge a second visual record.

        ``str.splitlines()`` breaks on U+0085, U+2028, and U+2029 even though the
        file holds a single ``\\n``. A smuggled separator in an MCP-controlled
        style would render a second ``[vibe-trace]`` line in a Unicode-aware tool.
        ``record`` escapes them, so the file is one physical line whose
        ``splitlines()`` yields one element and no raw separator byte on disk.
        """
        trace = VibeTraceLog(tmp_path / "vibe-trace.log")
        trace.record("style=a\u2028forged\u2029b\u0085c")

        text = trace.path.read_text(encoding="utf-8")
        assert text.splitlines() == [
            "[vibe-trace] style=a\\u2028forged\\u2029b\\u0085c"
        ]
        raw = trace.path.read_bytes()
        assert "\u2028".encode() not in raw
        assert "\u2029".encode() not in raw
        assert "\u0085".encode() not in raw

    def test_record_hardens_created_state_ancestors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On a fresh install every ancestor ``record`` creates lands 0o700.

        ``default()`` resolves ``<state>/logs/vibe-trace.log``. A bare
        ``mkdir(parents=True)`` would create the state root and vox dir at the
        umask default (~0o755), leaving per-user state world-traversable. The
        first ``record`` must leave state root, vox dir, and logs all private.
        """
        state_root = tmp_path / ".punt-labs" / "vox"
        monkeypatch.setattr("punt_vox.paths.user_state_dir", lambda: state_root)
        monkeypatch.setattr("punt_vox.vibe_trace.log_dir", paths.log_dir)

        VibeTraceLog.default().record("music off")

        created = (tmp_path / ".punt-labs", state_root, state_root / "logs")
        assert all((d.stat().st_mode & 0o077) == 0 for d in created)

    def test_record_tightens_loose_existing_file(self, tmp_path: Path) -> None:
        """A pre-existing group/other-readable log is re-tightened on append.

        ``os.open(mode=0o600)`` applies the mode only when it creates the file,
        so a log left 0o644 by a permissive umask or a prior run would keep
        appending readable. ``record`` fchmods the open fd to force 0o600.
        """
        log = tmp_path / "vibe-trace.log"
        log.touch()
        log.chmod(0o644)
        VibeTraceLog(log).record("music off")
        assert (log.stat().st_mode & 0o077) == 0

    def test_record_appends_when_file_chmod_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file we can't ``fchmod`` still gets its line -- privacy is best-effort.

        The fchmod tighten is defense-in-depth, not a precondition for the write:
        an ``os.fchmod`` that raises must be swallowed so the trace is never
        dropped, exactly as the directory hardening is.
        """

        def deny_fchmod(_fd: int, _mode: int) -> None:
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr("punt_vox.private_state.os.fchmod", deny_fchmod)
        trace = VibeTraceLog(tmp_path / "vibe-trace.log")

        trace.record("music off")

        assert trace.path.read_text(encoding="utf-8") == "[vibe-trace] music off\n"

    def test_is_writable_true_for_writable_dir(self, tmp_path: Path) -> None:
        """An absent log under a writable ancestor reports writable."""
        assert VibeTraceLog(tmp_path / "vibe-trace.log").is_writable() is True

    def test_is_writable_false_when_dir_lacks_execute(self, tmp_path: Path) -> None:
        """A write-only ancestor (0o200, no search bit) can't hold a new file.

        Creating a file in a directory needs the execute/search bit as well as
        write, so ``--w-------`` must report not-writable even though ``W_OK``
        alone would pass.
        """
        writeonly = tmp_path / "writeonly"
        writeonly.mkdir()
        writeonly.chmod(0o200)
        try:
            assert VibeTraceLog(writeonly / "vibe-trace.log").is_writable() is False
        finally:
            writeonly.chmod(0o700)

    def test_is_writable_false_when_dir_unwritable(self, tmp_path: Path) -> None:
        """A read-only ancestor -- one the log could never be created in -- is not."""
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        try:
            assert VibeTraceLog(locked / "vibe-trace.log").is_writable() is False
        finally:
            locked.chmod(0o700)

    def test_health_reports_relative_name_and_verdict(self, tmp_path: Path) -> None:
        """``health`` is the status-API view: relative name plus a live verdict.

        The path crosses as its bare name here (out of jail under tmp) -- never an
        absolute prefix -- and ``status`` is the healthy/degraded verdict.
        """
        trace = VibeTraceLog(tmp_path / "vibe-trace.log")
        assert trace.health() == {"name": "vibe-trace.log", "status": "healthy"}

    def test_is_writable_false_when_probe_raises_oserror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A traversal/permission failure is fail-safe: report False, never raise.

        ``Path.exists``/``is_file`` swallow a missing path but re-raise a
        ``PermissionError`` from an unreadable ancestor. A health check must not
        be able to crash on that, so an ``OSError`` from the probe means False.
        """
        trace = VibeTraceLog(tmp_path / "vibe-trace.log")

        def deny(_path: object, _mode: int) -> bool:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr("punt_vox.append_log.os.access", deny)
        assert trace.is_writable() is False

    def test_health_survives_probe_oserror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``health`` -- the status-API surface -- never propagates a probe error."""
        trace = VibeTraceLog(tmp_path / "vibe-trace.log")

        def blow_up(_path: object, _mode: int) -> bool:
            raise OSError(5, "I/O error")

        monkeypatch.setattr("punt_vox.append_log.os.access", blow_up)
        assert trace.health() == {"name": "vibe-trace.log", "status": "degraded"}

    def test_record_swallows_io_failure(self, tmp_path: Path) -> None:
        # A directory where the file should be makes os.open raise; the sink must
        # log-and-swallow so a trace never crashes the path it observes.
        clash = tmp_path / "vibe-trace.log"
        clash.mkdir()
        VibeTraceLog(clash).record("music off")  # no exception

    def test_concurrent_appends_never_tear(self, tmp_path: Path) -> None:
        """Many processes appending concurrently produce only whole, ordered lines.

        The O_APPEND single-write contract means no line from one process ever
        interleaves into another's. A torn write would leave a line that fails
        the fixed-shape pattern; a lost write would drop the total count.
        """
        path = tmp_path / "vibe-trace.log"
        procs, per_proc = 6, 200
        ctx = multiprocessing.get_context("spawn")
        workers = [
            ctx.Process(target=_append_many, args=(str(path), pid, per_proc))
            for pid in range(procs)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=60)
            assert worker.exitcode == 0

        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == procs * per_proc
        assert all(_LINE.match(line) for line in lines)
