"""Tests for punt_vox.voxd.record_store -- containment + atomic placement.

The containment tests are the vox-zu39 (P1) security assertions: a wire client
supplies at most a bare name, and no name -- absolute, separated, traversing,
empty, or NUL-bearing -- can cause a write outside the daemon-owned root.
"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from punt_vox.types import generate_filename
from punt_vox.voxd.record_store import RecordStore


@pytest.fixture
def store(tmp_path: Path) -> RecordStore:
    """A store rooted at an isolated recordings directory."""
    return RecordStore(tmp_path / "recordings")


# Hostile names a wire client must never be able to turn into a write path.
_HOSTILE_NAMES = [
    "/etc/passwd",
    "/tmp/pwned.mp3",
    "../../../etc/cron.d/x",
    "../secret.mp3",
    "sub/dir/out.mp3",
    "a\\b.mp3",
    "..",
    ".",
    "",
    "bad\x00name.mp3",
    "bad\nname.mp3",
    "tab\tname.mp3",
    "esc\x1bname.mp3",
]


class TestContainment:
    """No client-supplied name escapes the recordings root (vox-zu39, P1)."""

    def test_wire_absolute_path_rejected(self, store: RecordStore) -> None:
        with pytest.raises(ValueError, match="absolute"):
            store.resolve("/etc/passwd", "x")

    def test_wire_traversal_rejected(self, store: RecordStore) -> None:
        # A separator-bearing traversal is caught as a separator; a bare ".."
        # is caught as a dir token. Both must be refused.
        with pytest.raises(ValueError, match="separator"):
            store.resolve("../../../etc/cron.d/x", "x")
        with pytest.raises(ValueError, match="filename"):
            store.resolve("..", "x")

    def test_wire_separator_in_name_rejected(self, store: RecordStore) -> None:
        with pytest.raises(ValueError, match="separator"):
            store.resolve("a/b.mp3", "x")
        with pytest.raises(ValueError, match="separator"):
            store.resolve("a\\b.mp3", "x")

    def test_empty_and_nul_names_rejected(self, store: RecordStore) -> None:
        with pytest.raises(ValueError, match="NUL"):
            store.resolve("bad\x00name.mp3", "x")
        with pytest.raises(ValueError, match="empty"):
            store.resolve_ref("")

    def test_control_char_names_rejected(self, store: RecordStore) -> None:
        """A newline/tab/escape in a name is refused before any filesystem touch.

        Such a name would inject into the operator's log or terminal via the
        record locator. resolve and resolve_ref share the validator, so record
        naming and play/fetch refs both reject it.
        """
        with pytest.raises(ValueError, match="non-printable"):
            store.resolve("bad\nname.mp3", "x")
        with pytest.raises(ValueError, match="non-printable"):
            store.resolve_ref("esc\x1bname.mp3")

    def test_empty_name_rejected_by_store(self, store: RecordStore) -> None:
        """An explicit empty name is invalid; only None content-addresses."""
        with pytest.raises(ValueError, match="empty"):
            store.resolve("", "some text")
        # None still content-addresses (the daemon-generated no-name path).
        assert store.resolve(None, "some text").name.endswith(".mp3")

    @pytest.mark.parametrize("hostile", _HOSTILE_NAMES)
    def test_write_cannot_escape_root(self, store: RecordStore, hostile: str) -> None:
        """Property: every hostile name is rejected; none resolves outside root."""
        with pytest.raises(ValueError, match=r"recording name|empty|NUL"):
            store.resolve_ref(hostile)

    def test_token_does_not_grant_fs_write(
        self, store: RecordStore, tmp_path: Path
    ) -> None:
        """A place() with a hostile name writes nothing outside the root."""
        src = tmp_path / "src.mp3"
        src.write_bytes(b"\xff\xfb" * 50)
        target = tmp_path / "outside.mp3"

        with pytest.raises(ValueError, match="absolute"):
            store.place(source=src, text="x", name=str(target), cached=False)
        assert not target.exists()

    def test_default_name_is_content_addressed(self, store: RecordStore) -> None:
        resolved = store.resolve(None, "some text")
        assert resolved == (store.root / generate_filename("some text")).resolve()

    def test_bare_name_lands_in_root(self, store: RecordStore) -> None:
        resolved = store.resolve("greeting.mp3", "x")
        assert resolved.parent == store.root.resolve()
        assert resolved.name == "greeting.mp3"


class TestEnumerateAndRemove:
    """entries() lists immediate in-root files; remove() unlinks one by bare name."""

    def test_entries_skips_dirs_and_symlinks(
        self, store: RecordStore, tmp_path: Path
    ) -> None:
        store.root.mkdir(parents=True)
        (store.root / "real.mp3").write_bytes(b"12345")
        (store.root / "sub").mkdir()  # a directory is not a recording
        (store.root / "link.mp3").symlink_to(tmp_path / "elsewhere.mp3")
        names = {entry.name for entry in store.entries()}
        assert names == {"real.mp3"}

    def test_entries_does_not_follow_symlink_to_real_file(
        self, store: RecordStore, tmp_path: Path
    ) -> None:
        """A symlink to a real regular file is classified by lstat, never followed.

        The weaker sibling test uses a broken symlink, which a follow-based
        ``is_file()`` would also skip -- so it cannot tell a follow from a
        non-follow. Here the target is a genuine regular file: if ``entries()``
        followed the link (``is_file()`` semantics) it would list ``link.mp3`` as
        a plain recording. Because it classifies from ``lstat`` (whose mode is
        ``S_ISLNK``, not ``S_ISREG``), the link is excluded and its target is
        never probed out of the root.
        """
        store.root.mkdir(parents=True)
        target = tmp_path / "outside_target.mp3"
        target.write_bytes(b"real-bytes")  # a genuine regular file outside root
        (store.root / "real.mp3").write_bytes(b"12345")
        (store.root / "link.mp3").symlink_to(target)

        names = {entry.name for entry in store.entries()}

        assert names == {"real.mp3"}  # link.mp3 not followed, not listed

    def test_entries_reports_byte_counts(self, store: RecordStore) -> None:
        store.root.mkdir(parents=True)
        (store.root / "a.mp3").write_bytes(b"1234")
        [entry] = store.entries()
        assert entry.name == "a.mp3"
        assert entry.byte_count == 4

    def test_entries_skips_a_child_whose_stat_fails(
        self, store: RecordStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A child unlinked between iterdir and lstat (TOCTOU) is skipped, not fatal.

        A listing is best-effort under concurrent mutation: one entry whose lstat
        raises OSError drops out and the enumeration continues for the rest.
        """
        store.root.mkdir(parents=True)
        (store.root / "good.mp3").write_bytes(b"12345")
        (store.root / "racing.mp3").write_bytes(b"vanishing")
        real_lstat = Path.lstat

        def flaky_lstat(self: Path) -> object:
            if self.name == "racing.mp3":
                raise OSError("vanished mid-scan")
            return real_lstat(self)

        monkeypatch.setattr(Path, "lstat", flaky_lstat)
        names = {entry.name for entry in store.entries()}
        assert names == {"good.mp3"}  # the racing entry skipped, listing survives

    def test_entries_skips_names_a_ref_would_reject(self, store: RecordStore) -> None:
        """A planted file whose name BareName refuses is not listed.

        entries() must surface only names resolve_ref/remove would accept, so
        list and operate stay consistent -- a backslash- or non-printable-bearing
        name that ``rec get``/``rec remove`` reject can never appear in ``rec
        list``. A normal recording alongside it still lists.
        """
        store.root.mkdir(parents=True)
        (store.root / "good.mp3").write_bytes(b"12345")
        (store.root / "bad\\name.mp3").write_bytes(b"planted")  # backslash: rejected
        (store.root / "tab\tname.mp3").write_bytes(b"planted")  # non-printable
        names = {entry.name for entry in store.entries()}
        assert names == {"good.mp3"}

    def test_remove_unlinks_an_in_root_file(self, store: RecordStore) -> None:
        store.root.mkdir(parents=True)
        (store.root / "gone.mp3").write_bytes(b"x")
        store.remove("gone.mp3")
        assert not (store.root / "gone.mp3").exists()

    def test_remove_missing_raises_file_not_found(self, store: RecordStore) -> None:
        store.root.mkdir(parents=True)
        with pytest.raises(FileNotFoundError, match="no recording named"):
            store.remove("nope.mp3")

    def test_remove_hostile_ref_raises_before_touch(self, store: RecordStore) -> None:
        with pytest.raises(ValueError, match="separator"):
            store.remove("../../etc/passwd")

    def test_remove_symlink_deletes_link_leaves_in_root_target(
        self, store: RecordStore
    ) -> None:
        """remove() of a symlink entry unlinks the link, never its in-root target.

        ``resolve_ref`` follows symlinks (``.resolve()``); if ``remove`` reused it,
        removing ``link.mp3`` would delete the real recording it points at -- data
        loss. ``remove`` resolves the bare name *without* following, so the link
        itself is unlinked and ``real.mp3`` survives.
        """
        store.root.mkdir(parents=True)
        (store.root / "real.mp3").write_bytes(b"keep-me")
        (store.root / "link.mp3").symlink_to(store.root / "real.mp3")

        store.remove("link.mp3")

        assert not (store.root / "link.mp3").is_symlink()  # the link is gone
        assert (store.root / "real.mp3").read_bytes() == b"keep-me"  # target intact

    def test_remove_symlink_never_deletes_external_target(
        self, store: RecordStore, tmp_path: Path
    ) -> None:
        """A symlink to a file outside the root: remove deletes only the link.

        The no-follow removal cannot reach a path the client could never have
        named, so a delete never escapes the store.
        """
        store.root.mkdir(parents=True)
        outside = tmp_path / "outside.mp3"
        outside.write_bytes(b"external")
        (store.root / "escape.mp3").symlink_to(outside)

        store.remove("escape.mp3")

        assert not (store.root / "escape.mp3").is_symlink()  # link removed
        assert outside.read_bytes() == b"external"  # external file untouched

    def test_remove_broken_symlink_deletes_link(self, store: RecordStore) -> None:
        """A broken symlink is still an entry: remove unlinks it, does not raise.

        ``is_file`` follows and would report a broken link as absent; the
        ``is_symlink`` check accepts it so the dangling link can be cleaned up.
        """
        store.root.mkdir(parents=True)
        (store.root / "dangling.mp3").symlink_to(store.root / "missing.mp3")

        store.remove("dangling.mp3")

        assert not (store.root / "dangling.mp3").is_symlink()

    def test_entries_access_fault_propagates_not_empty(
        self, store: RecordStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A root that exists but cannot be stat'd raises, never a false empty list.

        A boolean ``is_dir()`` swallows the ``PermissionError`` and reports an
        empty store; classifying through PathStatus lets the fault propagate so
        ``RecListHandler`` turns it into a fault frame rather than silence.
        """
        store.root.mkdir(parents=True)
        real_stat = Path.stat

        def denied(self: Path, *, follow_symlinks: bool = True) -> object:
            if self == store.root:
                raise PermissionError(errno.EACCES, "permission denied")
            return real_stat(self, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(Path, "stat", denied)
        with pytest.raises(PermissionError, match="permission denied"):
            store.entries()

    def test_remove_access_fault_propagates_not_not_found(
        self, store: RecordStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A classify ``PermissionError`` propagates instead of reading "not found".

        ``is_symlink``/``is_file`` both answer False on ``EACCES``, mislabeling an
        access fault as a benign missing recording (a client-error rejection);
        classifying through PathStatus lets the ``OSError`` surface so
        ``RecRemoveHandler`` faults it.
        """
        store.root.mkdir(parents=True)
        target = store.root / "guarded.mp3"
        target.write_bytes(b"x")
        real_stat = Path.stat

        def denied(self: Path, *, follow_symlinks: bool = True) -> object:
            if self == target:
                raise PermissionError(errno.EACCES, "permission denied")
            return real_stat(self, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(Path, "stat", denied)
        with pytest.raises(PermissionError, match="permission denied"):
            store.remove("guarded.mp3")


class TestPlacement:
    """place() lands audio atomically in the root and reports its size."""

    def test_named_write_lands_bytes(self, store: RecordStore, tmp_path: Path) -> None:
        src = tmp_path / "src.mp3"
        src.write_bytes(b"\xff\xfb" * 100)

        write = store.place(source=src, text="hello", name="out.mp3", cached=False)

        assert write.path == (store.root / "out.mp3").resolve()
        assert write.path.read_bytes() == b"\xff\xfb" * 100
        assert write.byte_count == 200

    def test_default_names_by_content_hash(
        self, store: RecordStore, tmp_path: Path
    ) -> None:
        src = tmp_path / "src.mp3"
        src.write_bytes(b"\x00\x01\x02")

        write = store.place(source=src, text="some text", name=None, cached=False)

        assert write.path == (store.root / generate_filename("some text")).resolve()
        assert write.path.exists()

    def test_cached_source_is_preserved(
        self, store: RecordStore, tmp_path: Path
    ) -> None:
        src = tmp_path / "cache_entry.mp3"
        src.write_bytes(b"cached")

        store.place(source=src, text="hi", name="out.mp3", cached=True)

        assert src.exists()

    def test_fresh_source_is_removed(self, store: RecordStore, tmp_path: Path) -> None:
        src = tmp_path / "ephemeral.mp3"
        src.write_bytes(b"fresh")

        store.place(source=src, text="hi", name="out.mp3", cached=False)

        assert not src.exists()

    def test_move_reports_this_calls_bytes_not_a_racing_write(
        self, store: RecordStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reported size is the bytes this call wrote, not a racing dest write."""
        src = tmp_path / "mine.mp3"
        src.write_bytes(b"mine!")  # 5 bytes -- what THIS call lands

        real_replace = Path.replace

        def racing_replace(self: Path, target: str | Path) -> Path:
            # A concurrent same-name write lands a larger file at dest right after
            # our rename; a byte count read from dest afterwards would misreport it.
            result = real_replace(self, target)
            Path(target).write_bytes(b"someone-elses-larger-bytes")
            return result

        monkeypatch.setattr(Path, "replace", racing_replace)

        write = store.place(source=src, text="t", name="out.mp3", cached=False)

        assert write.byte_count == 5

        assert not src.exists()

    def test_fresh_uses_move_and_lands_0600(
        self, store: RecordStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fresh synthesis takes the atomic-move path (no copy) at 0600."""

        def no_copy(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("fresh synthesis must move, not copy")

        monkeypatch.setattr("punt_vox.voxd.record_store.shutil.copyfileobj", no_copy)

        src = tmp_path / "ephemeral.mp3"
        src.write_bytes(b"fresh-audio")
        src.chmod(0o644)
        write = store.place(source=src, text="hi", name="out.mp3", cached=False)

        assert write.path.read_bytes() == b"fresh-audio"
        assert write.path.stat().st_mode & 0o777 == 0o600
        assert not src.exists()

    def test_cross_device_falls_back_to_copy(
        self, store: RecordStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the atomic move can't cross filesystems, copy still lands 0600."""
        src = tmp_path / "ephemeral.mp3"
        src.write_bytes(b"xdev-audio")
        original_replace = Path.replace

        def selective_replace(self: Path, target: Path) -> Path:
            if self == src:
                raise OSError(errno.EXDEV, "cross-device link")
            return original_replace(self, target)

        monkeypatch.setattr(Path, "replace", selective_replace)

        write = store.place(source=src, text="hi", name="out.mp3", cached=False)

        assert write.path.read_bytes() == b"xdev-audio"
        assert write.path.stat().st_mode & 0o777 == 0o600
        assert not src.exists()

    def test_non_exdev_move_error_propagates(
        self, store: RecordStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-EXDEV OSError from the move re-raises; no silent copy fallback."""
        src = tmp_path / "ephemeral.mp3"
        src.write_bytes(b"nope")

        def denied_replace(self: Path, target: Path) -> Path:
            raise OSError(errno.EACCES, "permission denied")

        monkeypatch.setattr(Path, "replace", denied_replace)

        with pytest.raises(OSError, match="permission denied"):
            store.place(source=src, text="hi", name="out.mp3", cached=False)

        assert not (store.root / "out.mp3").exists()
        assert not list(store.root.glob("*.mp3.tmp"))

    def test_cached_copy_lands_0600_and_preserves_source(
        self, store: RecordStore, tmp_path: Path
    ) -> None:
        src = tmp_path / "cache_entry.mp3"
        src.write_bytes(b"cached-audio")

        write = store.place(source=src, text="hi", name="out.mp3", cached=True)

        assert src.exists()
        assert write.path.read_bytes() == b"cached-audio"
        assert write.path.stat().st_mode & 0o777 == 0o600

    def test_missing_source_leaves_no_partial_file(
        self, store: RecordStore, tmp_path: Path
    ) -> None:
        with pytest.raises(OSError, match="No such file"):
            store.place(
                source=tmp_path / "missing.mp3", text="hi", name="out.mp3", cached=False
            )

        assert not (store.root / "out.mp3").exists()
        assert not list(store.root.glob("*.mp3.tmp"))
