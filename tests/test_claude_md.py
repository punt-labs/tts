"""Tests for the bare ``@``-import writer (``ClaudeMdImport``)."""

from __future__ import annotations

import fcntl
import stat
from pathlib import Path
from typing import IO

import pytest

from punt_vox.claude_md import ClaudeMdImport

# Both scopes the writer serves: the repo-scope line the mission adds and the
# user-scope line install already registers.
_REPO = "@.punt-labs/vox/CLAUDE.md"
_USER = "@~/.punt-labs/vox/CLAUDE.md"


def _writer(tmp_path: Path, import_line: str = _REPO) -> ClaudeMdImport:
    return ClaudeMdImport(tmp_path / "CLAUDE.md", import_line)


# ---------------------------------------------------------------------------
# register — append-if-absent, idempotent (AppendImport 0->1->1)
# ---------------------------------------------------------------------------


def test_register_creates_file_with_bare_line(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    assert writer.register() is True
    text = writer.path.read_text(encoding="utf-8")
    assert text == f"{_REPO}\n"


def test_register_appends_to_existing_content(tmp_path: Path) -> None:
    host = tmp_path / "CLAUDE.md"
    host.write_text("# my rules\n\nkeep me\n", encoding="utf-8")
    assert _writer(tmp_path).register() is True
    assert host.read_text(encoding="utf-8") == f"# my rules\n\nkeep me\n{_REPO}\n"


def test_second_register_is_a_no_op(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    assert writer.register() is True
    first = writer.path.read_text(encoding="utf-8")
    mtime = writer.path.stat().st_mtime_ns
    # AppendImport is 0->1->1: the second enable adds nothing and rewrites nothing.
    assert writer.register() is False
    assert writer.path.read_text(encoding="utf-8") == first
    assert writer.path.stat().st_mtime_ns == mtime


def test_register_only_ever_writes_one_line(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.register()
    writer.register()
    writer.register()
    text = writer.path.read_text(encoding="utf-8")
    assert text.count(_REPO) == 1


def test_no_trailing_whitespace_and_separated_from_last_line(tmp_path: Path) -> None:
    host = tmp_path / "CLAUDE.md"
    # No final newline: register must add a separator so the import is not glued
    # to the user's last line.
    host.write_text("last line no newline", encoding="utf-8")
    assert _writer(tmp_path).register() is True
    assert host.read_text(encoding="utf-8") == f"last line no newline\n{_REPO}\n"


def test_is_registered_reflects_presence(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    assert writer.is_registered() is False
    writer.register()
    assert writer.is_registered() is True
    writer.prune()
    assert writer.is_registered() is False


def test_user_scope_line_is_also_supported(tmp_path: Path) -> None:
    writer = _writer(tmp_path, _USER)
    assert writer.register() is True
    assert writer.path.read_text(encoding="utf-8") == f"{_USER}\n"
    assert writer.prune() is True


# ---------------------------------------------------------------------------
# prune — remove every match (RemoveImport 2->0)
# ---------------------------------------------------------------------------


def test_prune_removes_the_line(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.register()
    assert writer.prune() is True
    assert writer.path.read_text(encoding="utf-8") == ""


def test_prune_absent_line_is_a_no_op(tmp_path: Path) -> None:
    host = tmp_path / "CLAUDE.md"
    host.write_text("# rules\n", encoding="utf-8")
    assert _writer(tmp_path).prune() is False
    assert host.read_text(encoding="utf-8") == "# rules\n"


def test_prune_missing_file_is_a_no_op(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    assert writer.prune() is False
    assert not writer.path.exists()


def test_prune_collapses_a_preexisting_duplicate(tmp_path: Path) -> None:
    # A racing or non-conformant writer could leave two copies; disable heals it
    # (RemoveImport 2->0). register never grows a duplicate on its own, so this
    # duplicate is planted directly.
    host = tmp_path / "CLAUDE.md"
    host.write_text(f"# rules\n{_REPO}\nmore\n{_REPO}\n", encoding="utf-8")
    assert _writer(tmp_path).prune() is True
    assert host.read_text(encoding="utf-8") == "# rules\nmore\n"


def test_prune_keeps_unrelated_imports(tmp_path: Path) -> None:
    host = tmp_path / "CLAUDE.md"
    host.write_text(f"@~/other.md\n{_REPO}\n", encoding="utf-8")
    assert _writer(tmp_path).prune() is True
    assert host.read_text(encoding="utf-8") == "@~/other.md\n"


# ---------------------------------------------------------------------------
# code blocks — a fenced or indented copy is inert
# ---------------------------------------------------------------------------


def test_fenced_line_is_not_matched_or_pruned(tmp_path: Path) -> None:
    host = tmp_path / "CLAUDE.md"
    fenced = f"# doc\n\n```\n{_REPO}\n```\n"
    host.write_text(fenced, encoding="utf-8")
    writer = _writer(tmp_path)
    # The only occurrence is fenced, so it is invisible to the writer.
    assert writer.is_registered() is False
    assert writer.prune() is False
    assert host.read_text(encoding="utf-8") == fenced
    # register appends a real top-level line, leaving the inert fenced copy.
    assert writer.register() is True
    text = host.read_text(encoding="utf-8")
    assert text == f"{fenced}{_REPO}\n"
    assert text.count(_REPO) == 2


def test_indented_line_is_not_matched_or_pruned(tmp_path: Path) -> None:
    host = tmp_path / "CLAUDE.md"
    indented = f"# doc\n\n    {_REPO}\n"
    host.write_text(indented, encoding="utf-8")
    writer = _writer(tmp_path)
    assert writer.is_registered() is False
    assert writer.prune() is False
    assert writer.register() is True
    assert host.read_text(encoding="utf-8") == f"{indented}{_REPO}\n"


def test_dangling_opener_above_the_import_does_not_hide_it(tmp_path: Path) -> None:
    # The unterminated-trailing-opener guard: a stray fence in the user's prose
    # above a column-0 import must NOT swallow it. The naive odd-count rule would
    # misclassify the import as fenced -- register would append a duplicate and
    # prune could not remove it (a 404ing @-import that loads every session).
    host = tmp_path / "CLAUDE.md"
    host.write_text(f"# rules\n\n```text\ndangling\n{_REPO}\n", encoding="utf-8")
    writer = _writer(tmp_path)
    # The import below the dangling opener is still seen ...
    assert writer.is_registered() is True
    # ... so a second register is a no-op (no duplicate) ...
    assert writer.register() is False
    # ... and prune removes it, leaving the dangling fence and its content.
    assert writer.prune() is True
    assert host.read_text(encoding="utf-8") == "# rules\n\n```text\ndangling\n"


# ---------------------------------------------------------------------------
# byte-preserving, host EOL for the appended line (LF / CRLF / lone-CR)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host_bytes",
    [
        pytest.param(b"# rules\n\nkeep me\n", id="lf"),
        pytest.param(b"# a\n# b\n\n# c\n", id="lf-multi"),
        pytest.param(b"# rules\r\n\r\nkeep me\r\n", id="crlf"),
        pytest.param(b"# a\r\n# b\r\n\r\n# c\r\n", id="crlf-multi"),
        pytest.param(b"# rules\rkeep me\r", id="cr"),
        pytest.param(b"# a\r# b\r\r# c\r", id="cr-multi"),
    ],
)
def test_register_then_prune_is_byte_identical(
    tmp_path: Path, host_bytes: bytes
) -> None:
    # Every byte outside the single import line is identical before and after a
    # register->prune round-trip, whatever the host's line endings. read/write
    # never apply universal-newline translation, so a Windows- or old-Mac-authored
    # CLAUDE.md is restored exactly.
    host = tmp_path / "CLAUDE.md"
    host.write_bytes(host_bytes)
    writer = _writer(tmp_path)
    assert writer.register() is True
    assert writer.prune() is True
    assert host.read_bytes() == host_bytes


@pytest.mark.parametrize(
    ("host_bytes", "eol"),
    [
        pytest.param(b"# rules\n", b"\n", id="lf"),
        pytest.param(b"# rules\r\n", b"\r\n", id="crlf"),
        pytest.param(b"# rules\r", b"\r", id="cr"),
    ],
)
def test_appended_line_uses_host_eol(
    tmp_path: Path, host_bytes: bytes, eol: bytes
) -> None:
    # The appended import uses the host's existing EOL, so it matches the
    # surrounding endings and stays terminator-insensitively matchable on re-run.
    host = tmp_path / "CLAUDE.md"
    host.write_bytes(host_bytes)
    assert _writer(tmp_path).register() is True
    after = host.read_bytes()
    assert after == host_bytes + _REPO.encode() + eol


def test_crlf_host_is_matched_and_pruned(tmp_path: Path) -> None:
    # A CRLF host leaves the import line as ``...CLAUDE.md\r``. Matching must be
    # terminator-insensitive, or the trailing ``\r`` defeats a byte-exact compare
    # -- register would duplicate and prune would silently fail.
    host = tmp_path / "CLAUDE.md"
    host.write_bytes(f"# rules\r\n{_REPO}\r\n".encode())
    writer = _writer(tmp_path)
    assert writer.is_registered() is True
    assert writer.register() is False
    assert writer.prune() is True
    assert host.read_bytes() == b"# rules\r\n"


# ---------------------------------------------------------------------------
# symlinked host (dotfile managers), atomic write, mode
# ---------------------------------------------------------------------------


def test_symlink_host_is_updated_and_link_preserved(tmp_path: Path) -> None:
    # Dotfile managers make CLAUDE.md a symlink into their store. The writer must
    # follow the link and rewrite the real file, never clobbering the link.
    store = tmp_path / "store"
    store.mkdir()
    real = store / "CLAUDE.md"
    real.write_text("# rules\n\nkeep me\n", encoding="utf-8")
    link = tmp_path / ".claude" / "CLAUDE.md"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)

    writer = ClaudeMdImport(link, _REPO)
    assert writer.register() is True

    assert link.is_symlink()
    assert link.readlink() == real
    real_text = real.read_text(encoding="utf-8")
    assert "keep me" in real_text
    assert real_text.endswith(f"{_REPO}\n")
    assert list(link.parent.glob(".*.tmp")) == []
    assert list(real.parent.glob(".*.tmp")) == []


def test_new_host_gets_0644(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    assert writer.register() is True
    assert stat.S_IMODE(writer.path.stat().st_mode) == 0o644


def test_existing_host_mode_is_preserved(tmp_path: Path) -> None:
    host = tmp_path / "CLAUDE.md"
    host.write_text("# rules\n", encoding="utf-8")
    host.chmod(0o600)
    assert _writer(tmp_path).register() is True
    assert stat.S_IMODE(host.stat().st_mode) == 0o600


def test_write_uses_temp_then_replace_not_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = tmp_path / "CLAUDE.md"
    host.write_text("# original\n", encoding="utf-8")
    seen: dict[str, Path] = {}
    real_replace = Path.replace

    def spy_replace(self: Path, target: Path) -> Path:
        seen["src"], seen["dst"] = self, target
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", spy_replace)
    assert _writer(tmp_path).register() is True
    # The rename source is a sibling temp; the destination is the host itself.
    assert seen["dst"] == host
    assert seen["src"].parent == host.parent
    assert seen["src"] != host


# ---------------------------------------------------------------------------
# the sibling lock — taken for the RMW, never the target itself
# ---------------------------------------------------------------------------


def test_register_locks_the_sibling_not_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = tmp_path / "CLAUDE.md"
    locked: list[Path] = []
    real_flock = fcntl.flock

    def spy_flock(fileobj: IO[str], operation: int) -> None:
        # Record the path flock is applied to (only on the exclusive acquire).
        if operation == fcntl.LOCK_EX:
            locked.append(Path(fileobj.name))
        real_flock(fileobj, operation)

    monkeypatch.setattr("punt_vox.sibling_lock.fcntl.flock", spy_flock)
    assert _writer(tmp_path).register() is True

    lock = host.parent / ".CLAUDE.md.punt-import.lock"
    # The sibling lock was taken; the target's own inode was never locked (the
    # atomic rename would strand a lock held on it).
    assert lock in locked
    assert host not in locked
    assert lock.exists()


def test_prune_takes_the_sibling_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = tmp_path / "CLAUDE.md"
    host.write_text(f"{_REPO}\n", encoding="utf-8")
    locked: list[Path] = []
    real_flock = fcntl.flock

    def spy_flock(fileobj: IO[str], operation: int) -> None:
        if operation == fcntl.LOCK_EX:
            locked.append(Path(fileobj.name))
        real_flock(fileobj, operation)

    monkeypatch.setattr("punt_vox.sibling_lock.fcntl.flock", spy_flock)
    assert _writer(tmp_path).prune() is True
    assert host.parent / ".CLAUDE.md.punt-import.lock" in locked
    assert host not in locked


# ---------------------------------------------------------------------------
# import-line validation at the construction boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_line",
    [
        "",  # empty
        "   ",  # whitespace only
        "\n",  # newline only
        "@a.md\n@b.md",  # embedded newline -> two lines
        "@a.md\n",  # trailing newline
        "@a.md\r\n",  # trailing CRLF
        "a\rb",  # embedded carriage return
        " @a.md",  # leading whitespace
        "@a.md ",  # trailing whitespace
        "\t@a.md",  # leading tab
        "a.md",  # missing @ prefix
        "# not an import",  # missing @ prefix, stray markdown
    ],
)
def test_construction_rejects_malformed_import_line(
    tmp_path: Path, bad_line: str
) -> None:
    with pytest.raises(ValueError):
        ClaudeMdImport(tmp_path / "CLAUDE.md", bad_line)


def test_valid_lines_are_accepted(tmp_path: Path) -> None:
    for line in (_REPO, _USER):
        writer = ClaudeMdImport(tmp_path / "CLAUDE.md", line)
        assert writer.import_line == line
