"""Teardown idempotence -- evidence item 4, proven without real forks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from teardown import Teardown

if TYPE_CHECKING:
    from pathlib import Path


class TestTeardownIdempotence:
    """Two consecutive runs both succeed; the second finds nothing."""

    def test_removes_scratch_root_then_reports_absent(self, tmp_path: Path) -> None:
        root = tmp_path / "scratch"
        (root / "proj").mkdir(parents=True)
        (root / "proj" / "junk.txt").write_text("x", encoding="utf-8")

        first = Teardown(root).run()
        assert not root.exists()
        assert any("removed scratch root" in line for line in first)

        second = Teardown(root).run()
        assert any("already absent" in line for line in second)

    def test_absent_root_and_no_sessions_is_a_clean_pass(self, tmp_path: Path) -> None:
        log = Teardown(tmp_path / "never-created").run()
        # tmux may or may not have a server running on this host; either way
        # no session carries the harness prefix, so nothing is killed.
        assert all(not line.startswith("killed") for line in log)
