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

    def test_entries_reports_byte_counts(self, store: RecordStore) -> None:
        store.root.mkdir(parents=True)
        (store.root / "a.mp3").write_bytes(b"1234")
        [entry] = store.entries()
        assert entry.name == "a.mp3"
        assert entry.byte_count == 4

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
