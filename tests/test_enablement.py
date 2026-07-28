"""Tests for the per-repo enablement state machine (``docs/vox-enable-disable.tex``).

The two load-bearing properties are asserted by name: the § 2.11 biconditional
(marker present iff exactly one canonical import) after every op, and the
no-orphan-on-purge property (purge removes the import, never stranding a 404ing
``@``-import).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from punt_vox.claude_md import ClaudeMdImport
from punt_vox.enablement import RepoEnablement
from punt_vox.settings_registration import SettingsRegistration

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


# ---------------------------------------------------------------------------
# Crash-safety: the marker is written LAST, so a partial enable leaves vox OFF
# ---------------------------------------------------------------------------


def _raise_oserror(*_args: object, **_kwargs: object) -> None:
    raise OSError("simulated step failure")


def _assert_partial_enable_writes_no_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: type,
    method: str,
) -> None:
    """A step failing mid-``enable`` must leave no marker (vox observably OFF).

    The marker is written last precisely so this holds: the hooks gate on the
    marker, so a crash mid-``enable`` degrades to OFF rather than half-on (a
    marker with no guidance behind it). The reverse residue -- an import already
    written when a later step fails -- is benign: the hooks ignore it, and a
    subsequent ``disable`` or a re-run of ``enable`` heals it. The § 2.11
    biconditional is a steady-state property of a *completed* transition, not of
    a crash, so it is not asserted here.
    """
    monkeypatch.setattr(target, method, _raise_oserror)
    with pytest.raises(OSError, match="simulated step failure"):
        RepoEnablement.for_repo(tmp_path).enable()
    assert not _marker_present(tmp_path)


def test_enable_leaves_no_marker_when_import_register_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_partial_enable_writes_no_marker(
        tmp_path, monkeypatch, ClaudeMdImport, "register"
    )


def test_enable_leaves_no_marker_when_settings_register_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_partial_enable_writes_no_marker(
        tmp_path, monkeypatch, SettingsRegistration, "register"
    )


# ---------------------------------------------------------------------------
# Symlink refusal: an untrusted repo cannot redirect a tool-owned write
# ---------------------------------------------------------------------------


def test_enable_refuses_symlink_at_marker_path_leaving_target_intact(
    tmp_path: Path,
) -> None:
    """A symlink planted at ``.punt-labs/vox/enabled`` is refused, not followed.

    Without the ``O_NOFOLLOW`` guard, ``enable`` would overwrite the symlink's
    *target* (e.g. ``~/.ssh/id_rsa``) with the marker text -- data destruction.
    """
    secret = tmp_path / "secret.txt"
    secret.write_text("PRIVATE KEY\n", encoding="utf-8")
    vox = tmp_path / ".punt-labs" / "vox"
    vox.mkdir(parents=True)
    (vox / "enabled").symlink_to(secret)

    with pytest.raises(ValueError, match="symlink at a tool-owned path"):
        RepoEnablement.for_repo(tmp_path).enable()

    assert (vox / "enabled").is_symlink()
    assert secret.read_text(encoding="utf-8") == "PRIVATE KEY\n"


def test_enable_refuses_symlink_at_guide_path_leaving_target_intact(
    tmp_path: Path,
) -> None:
    """A symlink planted at the deposited ``CLAUDE.md`` guide is refused.

    The guide is the first step of ``enable``, so its target is protected and the
    marker is never written (the repo stays observably OFF).
    """
    secret = tmp_path / "secret.txt"
    secret.write_text("PRIVATE KEY\n", encoding="utf-8")
    vox = tmp_path / ".punt-labs" / "vox"
    vox.mkdir(parents=True)
    (vox / "CLAUDE.md").symlink_to(secret)

    with pytest.raises(ValueError, match="symlink at a tool-owned path"):
        RepoEnablement.for_repo(tmp_path).enable()

    assert (vox / "CLAUDE.md").is_symlink()
    assert secret.read_text(encoding="utf-8") == "PRIVATE KEY\n"
    assert not _marker_present(tmp_path)


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
