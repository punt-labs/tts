"""Teardown idempotence and verified claims -- no real forks needed.

A teardown pass may only claim success it can verify: the scratch root
must actually be gone from disk, and a killed session must actually be
unknown to tmux. A pass that leaves anything behind reports it and is
not clean.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# NOTE: the alias must not be named `teardown_module` -- pytest's xunit
# protocol would treat that module-level name as its teardown hook.
import teardown as teardown_mod
from teardown import Teardown

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class TestTeardownIdempotence:
    """Two consecutive runs both succeed; the second finds nothing."""

    def test_removes_scratch_root_then_reports_absent(self, tmp_path: Path) -> None:
        root = tmp_path / "scratch"
        (root / "proj").mkdir(parents=True)
        (root / "proj" / "junk.txt").write_text("x", encoding="utf-8")

        first = Teardown(root).run()
        assert not root.exists()
        assert first.clean is True
        assert any("removed scratch root" in line for line in first.log)

        second = Teardown(root).run()
        assert second.clean is True
        assert any("already absent" in line for line in second.log)

    def test_absent_root_and_no_sessions_is_a_clean_pass(self, tmp_path: Path) -> None:
        outcome = Teardown(tmp_path / "never-created").run()
        # tmux may or may not have a server running on this host; either way
        # no session carries the harness prefix, so nothing is killed.
        assert outcome.clean is True
        assert all(not line.startswith("killed") for line in outcome.log)


class TestTeardownVerifiesItsClaims:
    """rmtree failure must surface as FAILED + not clean, never exit-0."""

    def test_surviving_scratch_root_is_reported_and_not_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "scratch"
        root.mkdir()
        (root / "credentials-copy.json").write_text("{}", encoding="utf-8")
        # Simulate an rmtree that silently fails (ignore_errors swallows
        # e.g. EACCES): the tree is still on disk afterwards.
        monkeypatch.setattr(
            teardown_mod.shutil, "rmtree", lambda *_args, **_kwargs: None
        )
        outcome = Teardown(root).run()
        assert outcome.clean is False
        assert any("FAILED to remove scratch root" in line for line in outcome.log)
        assert root.exists()
