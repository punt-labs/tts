"""Tests for the filesystem store (scan/open/create) and its in-memory parity."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import pytest

from punt_vox.voxd.programs import Part
from punt_vox.voxd.programs.album_id import AlbumId
from punt_vox.voxd.programs.album_tags import AlbumTags, PromptFingerprint
from punt_vox.voxd.programs.filesystem_store import (
    FilesystemPartStore,
    FilesystemProgramStore,
)
from punt_vox.voxd.programs.manifest import ManifestDraft, PartEntry
from punt_vox.voxd.programs.part import PartStatus
from punt_vox.voxd.programs.store import PartStore, ProgramStore

from .conftest import InMemoryProgramStore, make_manifest

EntryFactory = Callable[..., PartEntry]

_FINGERPRINT = PromptFingerprint("deadbeef")


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
