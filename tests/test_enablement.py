"""Tests for the per-repo enablement state machine (``docs/vox-enable-disable.tex``).

The two load-bearing properties are asserted by name: the § 2.11 biconditional
(marker present iff exactly one canonical import) after every op, and the
no-orphan-on-purge property (purge removes the import, never stranding a 404ing
``@``-import).
"""

from __future__ import annotations

import json
from pathlib import Path

from punt_vox.enablement import RepoEnablement

_IMPORT = "@.punt-labs/vox/CLAUDE.md"


def _import_count(repo: Path) -> int:
    """Count top-level occurrences of the canonical import in the repo CLAUDE.md."""
    host = repo / "CLAUDE.md"
    if not host.is_file():
        return 0
    return sum(
        1 for line in host.read_text(encoding="utf-8").splitlines() if line == _IMPORT
    )


def _marker_present(repo: Path) -> bool:
    return (repo / ".punt-labs" / "vox" / "enabled").is_file()


def _dir_present(repo: Path) -> bool:
    return (repo / ".punt-labs" / "vox").is_dir()


def _assert_biconditional(repo: Path) -> None:
    """The § 2.11 invariant: marker present iff exactly one import line."""
    if _marker_present(repo):
        assert _import_count(repo) == 1
    else:
        assert _import_count(repo) == 0


# ---------------------------------------------------------------------------
# Enable -- reach Enabled, idempotent
# ---------------------------------------------------------------------------


def test_enable_reaches_enabled(tmp_path: Path) -> None:
    RepoEnablement.for_repo(tmp_path).enable()
    assert _marker_present(tmp_path)
    assert _import_count(tmp_path) == 1
    assert (tmp_path / ".punt-labs" / "vox" / "CLAUDE.md").is_file()
    assert _dir_present(tmp_path)
    _assert_biconditional(tmp_path)


def test_enable_registers_settings(tmp_path: Path) -> None:
    RepoEnablement.for_repo(tmp_path).enable()
    settings = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert "Bash(vox:*)" in data["permissions"]["allow"]


def test_enable_is_idempotent_no_second_import(tmp_path: Path) -> None:
    enablement = RepoEnablement.for_repo(tmp_path)
    enablement.enable()
    enablement.enable()
    enablement.enable()
    # AppendImport is 0->1->1: the marked repo carries exactly one import.
    assert _import_count(tmp_path) == 1
    assert _marker_present(tmp_path)
    _assert_biconditional(tmp_path)


def test_enable_preserves_user_prose_in_claude_md(tmp_path: Path) -> None:
    host = tmp_path / "CLAUDE.md"
    host.write_text("# My rules\n\nKeep me.\n", encoding="utf-8")
    RepoEnablement.for_repo(tmp_path).enable()
    text = host.read_text(encoding="utf-8")
    assert text == f"# My rules\n\nKeep me.\n{_IMPORT}\n"


# ---------------------------------------------------------------------------
# Disable -- non-destructive, removes import + marker
# ---------------------------------------------------------------------------


def test_disable_removes_import_and_marker(tmp_path: Path) -> None:
    enablement = RepoEnablement.for_repo(tmp_path)
    enablement.enable()
    enablement.disable()
    assert not _marker_present(tmp_path)
    assert _import_count(tmp_path) == 0
    _assert_biconditional(tmp_path)


def test_disable_leaves_the_directory_dormant(tmp_path: Path) -> None:
    enablement = RepoEnablement.for_repo(tmp_path)
    enablement.enable()
    enablement.disable()
    # dirPresent' = dirPresent: enable created the dir, disable leaves it (Dormant).
    assert _dir_present(tmp_path)
    assert (tmp_path / ".punt-labs" / "vox" / "CLAUDE.md").is_file()


def test_disable_deregisters_settings(tmp_path: Path) -> None:
    enablement = RepoEnablement.for_repo(tmp_path)
    enablement.enable()
    enablement.disable()
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text("utf-8"))
    assert "Bash(vox:*)" not in data["permissions"]["allow"]


def test_disable_on_absent_repo_does_not_create_a_directory(tmp_path: Path) -> None:
    # Disable's frame: run on an already-Absent repo, it must not conjure an empty
    # .punt-labs/vox/ (a spurious Dormant state) -- dirPresent' = dirPresent.
    RepoEnablement.for_repo(tmp_path).disable()
    assert not _dir_present(tmp_path)
    assert not _marker_present(tmp_path)
    _assert_biconditional(tmp_path)


# ---------------------------------------------------------------------------
# Purge -- reach Absent, no orphan import
# ---------------------------------------------------------------------------


def test_purge_removes_the_subtree_and_the_import(tmp_path: Path) -> None:
    enablement = RepoEnablement.for_repo(tmp_path)
    enablement.enable()
    enablement.purge()
    assert not _dir_present(tmp_path)
    assert not _marker_present(tmp_path)
    assert _import_count(tmp_path) == 0
    _assert_biconditional(tmp_path)


def test_purge_leaves_no_orphan_import(tmp_path: Path) -> None:
    # The load-bearing no-orphan property: purge must remove the import (which
    # lives in CLAUDE.md, OUTSIDE the subtree) before deleting the guide file it
    # points at. A subtree-only purge would leave a 404ing @-import.
    enablement = RepoEnablement.for_repo(tmp_path)
    enablement.enable()
    enablement.purge()
    guide = tmp_path / ".punt-labs" / "vox" / "CLAUDE.md"
    assert not guide.is_file()
    # No import line survives that would point at the now-deleted guide.
    assert _import_count(tmp_path) == 0


def test_purge_preserves_user_prose(tmp_path: Path) -> None:
    host = tmp_path / "CLAUDE.md"
    host.write_text("# Rules\n\nKeep me.\n", encoding="utf-8")
    enablement = RepoEnablement.for_repo(tmp_path)
    enablement.enable()
    enablement.purge()
    assert host.read_text(encoding="utf-8") == "# Rules\n\nKeep me.\n"


# ---------------------------------------------------------------------------
# The full walk: every reachable state preserves the biconditional
# ---------------------------------------------------------------------------


def test_biconditional_holds_across_the_state_walk(tmp_path: Path) -> None:
    enablement = RepoEnablement.for_repo(tmp_path)
    _assert_biconditional(tmp_path)  # Absent
    enablement.enable()
    _assert_biconditional(tmp_path)  # Enabled
    enablement.disable()
    _assert_biconditional(tmp_path)  # Dormant
    enablement.enable()
    _assert_biconditional(tmp_path)  # Enabled again (upgrade path)
    enablement.purge()
    _assert_biconditional(tmp_path)  # Absent


def test_disable_heals_a_racing_writers_duplicate(tmp_path: Path) -> None:
    # RemoveImport 2->0: a non-conformant writer could leave two import lines;
    # disable removes every match, restoring the biconditional.
    host = tmp_path / "CLAUDE.md"
    host.write_text(f"# rules\n{_IMPORT}\nmore\n{_IMPORT}\n", encoding="utf-8")
    RepoEnablement.for_repo(tmp_path).disable()
    assert _import_count(tmp_path) == 0


def test_root_property_reports_the_repo_root(tmp_path: Path) -> None:
    assert RepoEnablement.for_repo(tmp_path).root == tmp_path


def test_marker_content_is_deterministic(tmp_path: Path) -> None:
    # The marker bytes must be identical everywhere so the CLI and MCP surfaces
    # write the same file (§ 2.14). Two independent enables produce equal bytes.
    other = tmp_path / "other"
    other.mkdir()
    RepoEnablement.for_repo(tmp_path).enable()
    RepoEnablement.for_repo(other).enable()
    a = (tmp_path / ".punt-labs" / "vox" / "enabled").read_bytes()
    b = (other / ".punt-labs" / "vox" / "enabled").read_bytes()
    assert a == b
