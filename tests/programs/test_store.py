"""Tests for the filesystem store (scan/open/create) and its in-memory parity."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import IO, Self, final

import pytest

from punt_vox.voxd.programs import Part
from punt_vox.voxd.programs.album_id import AlbumId
from punt_vox.voxd.programs.album_tags import AlbumTags, PromptFingerprint
from punt_vox.voxd.programs.filesystem_store import (
    _MAX_MANIFEST_BYTES,
    FilesystemPartStore,
    FilesystemProgramStore,
)
from punt_vox.voxd.programs.manifest import ManifestDraft, PartEntry
from punt_vox.voxd.programs.part import PartStatus
from punt_vox.voxd.programs.store import PartStore, ProgramStore

from .conftest import InMemoryProgramStore, make_manifest

EntryFactory = Callable[..., PartEntry]

_FINGERPRINT = PromptFingerprint("deadbeef")


@final
class _ReadSpy:
    """A binary file-handle wrapper that records the size of each read call.

    Used to prove ``_read_manifest_text`` bounds its read rather than pulling a
    whole file into memory: the manifest read must ask for at most one byte past
    the ceiling, regardless of how large the file on disk actually is.
    """

    _handle: IO[bytes]
    _sizes: list[int]

    def __new__(cls, handle: IO[bytes], sizes: list[int]) -> Self:
        self = super().__new__(cls)
        self._handle = handle
        self._sizes = sizes
        return self

    def read(self, size: int = -1) -> bytes:
        self._sizes.append(size)
        return self._handle.read(size)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self._handle.close()


def _draft(
    album_id: str = "a3f1c9",
    style: str = "techno",
    vibe: str = "ambient",
    *indices: int,
) -> ManifestDraft:
    """Build a draft for an album with ready Parts at ``indices``."""
    return ManifestDraft(
        album_id=AlbumId(album_id),
        tags=AlbumTags(style=style, vibe=vibe),
        fingerprint=_FINGERPRINT,
        parts=tuple(
            PartEntry(index=i, file=f"{i:03d}.mp3", status=PartStatus.READY)
            for i in indices
        ),
    )


class TestCreateAndScan:
    def test_create_then_scan_round_trips(self, tmp_path: Path) -> None:
        store = FilesystemProgramStore(tmp_path)
        draft = _draft("a3f1c9", "techno", "ambient", 1, 2)
        store.create(draft)
        albums = store.scan()
        assert len(albums) == 1
        assert albums[0].id == AlbumId("a3f1c9")
        assert albums[0].locator == draft.locator

    def test_scan_empty_root(self, tmp_path: Path) -> None:
        assert FilesystemProgramStore(tmp_path / "missing").scan() == ()

    def test_scan_skips_idless_legacy_dir(self, tmp_path: Path) -> None:
        # A pre-change directory with no id in its manifest is invisible to scan.
        legacy = tmp_path / "trance"
        legacy.mkdir(parents=True)
        (legacy / "manifest.json").write_text(
            '{"name": "trance", "format": "playlist", '
            '"subject": {"vibe": "trance", "style": "trance"}, "parts": []}',
            encoding="utf-8",
        )
        # A valid id-bearing album alongside it is the only one scanned.
        FilesystemProgramStore(tmp_path).create(_draft("a3f1c9", "lofi", "calm"))
        albums = FilesystemProgramStore(tmp_path).scan()
        assert [a.id.value for a in albums] == ["a3f1c9"]

    def test_scan_isolates_a_corrupt_manifest(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A truncated id-bearing manifest is a real fault, not an intentional skip:
        # scan logs it at ERROR and drops that one album, keeping the rest of the
        # catalog -- and the daemon that scans at boot -- alive.
        broken = tmp_path / "corrupt-dir"
        broken.mkdir(parents=True)
        (broken / "manifest.json").write_text('{"id": "bad123"}', encoding="utf-8")
        FilesystemProgramStore(tmp_path).create(_draft("a3f1c9", "lofi", "calm"))
        with caplog.at_level(logging.ERROR):
            albums = FilesystemProgramStore(tmp_path).scan()
        assert [a.id.value for a in albums] == ["a3f1c9"]  # the healthy album survives
        assert any("corrupt manifest" in r.getMessage() for r in caplog.records)

    def test_scan_skips_an_oversized_manifest(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A giant planted manifest is rejected before read_text pulls it into
        # memory, so one oversized file cannot OOM the daemon at boot scan; the
        # rest of the catalog survives.
        planted = tmp_path / "planted-dir"
        planted.mkdir(parents=True)
        (planted / "manifest.json").write_text(
            "x" * (1024 * 1024 + 1), encoding="utf-8"
        )
        FilesystemProgramStore(tmp_path).create(_draft("a3f1c9", "lofi", "calm"))
        with caplog.at_level(logging.ERROR):
            albums = FilesystemProgramStore(tmp_path).scan()
        assert [a.id.value for a in albums] == ["a3f1c9"]  # the healthy album survives
        assert any("exceeds" in r.getMessage() for r in caplog.records)

    def test_scan_skips_a_symlinked_manifest(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A manifest.json symlinked at an arbitrary target is a planted file: the
        # O_NOFOLLOW open refuses it rather than reading through the link, so scan
        # treats it as a corrupt album and the rest of the catalog survives.
        secret = tmp_path / "secret.json"
        secret.write_text('{"id": "sneaky"}', encoding="utf-8")
        planted = tmp_path / "planted-dir"
        planted.mkdir(parents=True)
        (planted / "manifest.json").symlink_to(secret)
        FilesystemProgramStore(tmp_path).create(_draft("a3f1c9", "lofi", "calm"))
        with caplog.at_level(logging.ERROR):
            albums = FilesystemProgramStore(tmp_path).scan()
        assert [a.id.value for a in albums] == ["a3f1c9"]  # the healthy album survives
        assert any("corrupt manifest" in r.getMessage() for r in caplog.records)

    def test_open_rejects_an_oversized_manifest(self, tmp_path: Path) -> None:
        """``open`` refuses a manifest above the ceiling before reading it."""
        store = FilesystemProgramStore(tmp_path)
        draft = _draft("a3f1c9", "techno", "ambient")
        store.create(draft)
        (tmp_path / draft.locator / "manifest.json").write_text(
            "x" * (1024 * 1024 + 1), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="exceeds"):
            store.open(draft.locator)

    def test_open_rejects_a_symlinked_manifest(self, tmp_path: Path) -> None:
        """``open`` refuses a symlinked manifest instead of reading its target."""
        store = FilesystemProgramStore(tmp_path)
        draft = _draft("a3f1c9", "techno", "ambient")
        store.create(draft)
        secret = tmp_path / "secret.json"
        secret.write_text('{"id": "sneaky"}', encoding="utf-8")
        manifest = tmp_path / draft.locator / "manifest.json"
        manifest.unlink()
        manifest.symlink_to(secret)
        with pytest.raises(OSError):
            store.open(draft.locator)

    def test_open_bounds_the_manifest_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The manifest read is capped at the ceiling+1, never the file's size.

        A trusted ``fstat`` size could be stale if the file grows between the
        stat and the read (TOCTOU); the fix reads a bounded slice and rejects on
        its length. Here a file eight times the ceiling is refused after a single
        ``read`` of exactly ceiling+1 bytes -- it is never pulled in whole.
        """
        store = FilesystemProgramStore(tmp_path)
        draft = _draft("a3f1c9", "techno", "ambient")
        store.create(draft)
        manifest = tmp_path / draft.locator / "manifest.json"
        manifest.write_bytes(b"x" * (_MAX_MANIFEST_BYTES * 8))

        requested: list[int] = []
        real_fdopen = os.fdopen

        def spy_fdopen(fd: int, mode: str = "rb") -> _ReadSpy:
            return _ReadSpy(real_fdopen(fd, mode), requested)

        monkeypatch.setattr(os, "fdopen", spy_fdopen)
        with pytest.raises(ValueError, match="exceeds"):
            store.open(draft.locator)
        assert requested == [_MAX_MANIFEST_BYTES + 1]

    def test_read_manifest_text_accepts_a_manifest_at_the_ceiling(
        self, tmp_path: Path
    ) -> None:
        """A file of exactly the ceiling in bytes is read in full, not rejected."""
        manifest = tmp_path / "manifest.json"
        manifest.write_bytes(b"x" * _MAX_MANIFEST_BYTES)
        text = FilesystemProgramStore._read_manifest_text(manifest)
        assert len(text) == _MAX_MANIFEST_BYTES

    def test_manifest_written_utf8(self, tmp_path: Path) -> None:
        store = FilesystemProgramStore(tmp_path)
        store.create(_draft("a3f1c9", "techno", "ambient", 1))
        text = (tmp_path / "techno--ambient-a3f1c9" / "manifest.json").read_text(
            encoding="utf-8"
        )
        assert '"id": "a3f1c9"' in text
        assert '"format": "playlist"' in text


class TestOpen:
    def test_open_reads_back_a_created_album(self, tmp_path: Path) -> None:
        store = FilesystemProgramStore(tmp_path)
        draft = _draft("a3f1c9", "techno", "ambient")
        part_store = store.create(draft)
        part_store.record(PartEntry(index=1, file="001.mp3", status=PartStatus.READY))
        reopened = store.open(draft.locator)
        assert reopened.ready_parts() == (Part("001.mp3", 1),)

    def test_open_absent_raises(self, tmp_path: Path) -> None:
        store = FilesystemProgramStore(tmp_path)
        with pytest.raises(LookupError, match="no saved album"):
            store.open("ghost-000000")

    def test_create_rejects_a_duplicate_directory(self, tmp_path: Path) -> None:
        store = FilesystemProgramStore(tmp_path)
        store.create(_draft("a3f1c9", "techno", "ambient"))
        with pytest.raises(FileExistsError):
            store.create(_draft("a3f1c9", "techno", "ambient"))  # same slug-id


class TestDelete:
    """``delete`` removes an album dir and tolerates an already-missing one."""

    def test_delete_removes_the_album_directory(self, tmp_path: Path) -> None:
        store = FilesystemProgramStore(tmp_path)
        draft = _draft("a3f1c9", "techno", "ambient", 1)
        store.create(draft)
        store.delete(draft.locator)
        assert not (tmp_path / draft.locator).exists()

    def test_delete_is_idempotent_when_directory_already_gone(
        self, tmp_path: Path
    ) -> None:
        # A missing directory is "already deleted", not an error, so a caller can
        # always forget its catalog entry after delete without a stale dir leaving
        # a ghost id. A second delete of the same locator is a clean no-op.
        store = FilesystemProgramStore(tmp_path)
        draft = _draft("a3f1c9", "techno", "ambient", 1)
        store.create(draft)
        store.delete(draft.locator)
        store.delete(draft.locator)  # already gone -- no FileNotFoundError

    def test_delete_still_guards_a_hostile_locator(self, tmp_path: Path) -> None:
        # Idempotence does not weaken the containment guard: a traversal locator is
        # refused before any unlink, even though the "directory" does not exist.
        store = FilesystemProgramStore(tmp_path / "root")
        with pytest.raises(ValueError, match="single path segment"):
            store.delete("../../etc")


class TestPartStore:
    def test_next_index_and_write_target(self, tmp_path: Path) -> None:
        store = FilesystemProgramStore(tmp_path)
        part_store = store.create(_draft("a3f1c9", "techno", "ambient", 1, 2))
        directory = tmp_path / "techno--ambient-a3f1c9"
        assert part_store.next_index() == 3
        assert part_store.write_target(3) == directory / "003.mp3"

    def test_directory_and_root_accessors(self, tmp_path: Path) -> None:
        assert FilesystemProgramStore(tmp_path).root == tmp_path
        part_store = FilesystemPartStore(tmp_path / "x", make_manifest(1))
        assert part_store.directory == tmp_path / "x"


class TestPathTraversalGuard:
    """A locator must be a single safe segment produced by scan()/create()."""

    @pytest.mark.parametrize(
        "locator",
        [
            "..",
            "../../etc",
            "a/b",
            "sub/mix-a3f1c9",
            "",
            ".",
            "./foo",  # normalizes to "foo" -- rejected as non-canonical
            "foo/",  # trailing separator -- rejected as non-canonical
        ],
    )
    def test_open_rejects_non_canonical_locator(
        self, tmp_path: Path, locator: str
    ) -> None:
        # Only a plain single segment (exactly as scan()/create() produce) is
        # accepted. Empty, ".", "..", multi-segment, and non-canonical spellings
        # that would silently normalize to a segment are all refused up front,
        # before the containment check (defense in depth).
        store = FilesystemProgramStore(tmp_path / "root")
        with pytest.raises(ValueError, match="single path segment"):
            store.open(locator)


class TestProtocolConformance:
    def test_filesystem_satisfies_protocols(self, tmp_path: Path) -> None:
        program_store: ProgramStore = FilesystemProgramStore(tmp_path)
        part_store: PartStore = program_store.create(_draft())
        assert isinstance(program_store, ProgramStore)
        assert isinstance(part_store, PartStore)

    def test_in_memory_satisfies_protocols(
        self, program_store: InMemoryProgramStore
    ) -> None:
        part_store: PartStore = program_store.create(_draft())
        store: ProgramStore = program_store
        assert isinstance(store, ProgramStore)
        assert isinstance(part_store, PartStore)


class TestParity:
    """The in-memory fake must behave like the filesystem store."""

    def test_create_then_scan_parity(
        self, tmp_path: Path, program_store: InMemoryProgramStore
    ) -> None:
        fs = FilesystemProgramStore(tmp_path)
        for store in (fs, program_store):
            store.create(_draft("a3f1c9", "techno", "ambient", 1))
        assert [a.id for a in fs.scan()] == [a.id for a in program_store.scan()]

    def test_delete_is_idempotent_parity(
        self, tmp_path: Path, program_store: InMemoryProgramStore
    ) -> None:
        # Both stores treat delete of an already-gone album as a clean no-op, so
        # the in-memory fake exercises the real idempotent contract rather than
        # diverging by raising LookupError on a missing directory.
        fs = FilesystemProgramStore(tmp_path)
        draft = _draft("a3f1c9", "techno", "ambient", 1)
        for store in (fs, program_store):
            store.create(draft)
            store.delete(draft.locator)
            store.delete(draft.locator)  # already gone -- no error
        assert fs.scan() == () == program_store.scan()
