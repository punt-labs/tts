"""Shared factory fixtures for the music-player tests: Albums and ProgramStatus.

Exposed as fixtures (not module imports) so mypy names this ``conftest`` once --
the repo's tests reach shared builders through fixtures, never ``from ...conftest
import`` (which would give the file two module names under the ``tests`` layout).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from punt_vox.types_programs.format import Format
from punt_vox.types_programs.identifiers import ProgramName
from punt_vox.types_programs.status import ProgramStatus
from punt_vox.types_programs.status_views import NowPlaying
from punt_vox.voxd.programs.album_id import AlbumId
from punt_vox.voxd.programs.album_tags import AlbumTags, PromptFingerprint
from punt_vox.voxd.programs.catalog import Album
from punt_vox.voxd.programs.manifest import AlbumManifest, ManifestDraft, PartEntry
from punt_vox.voxd.programs.part import Part, PartStatus
from punt_vox.voxd.programs.store import PartStore, ProgramStore

_FINGERPRINT = PromptFingerprint.from_prompts("base", ())
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def album_of() -> Callable[..., Album]:
    """Return the Album factory: ``album_of()(id, name=..., tracks=...)``."""
    return make_album


@pytest.fixture
def playing_of() -> Callable[[Album, int, int], ProgramStatus]:
    """Return the factory for the radio status of an album playing a track."""
    return playing_status


@pytest.fixture
def radio_of() -> Callable[[int, int], ProgramStatus]:
    """Return the factory for a multi-album radio status naming no single album."""
    return radio_status


def make_album(
    album_id: str,
    *,
    name: str | None = None,
    tracks: int = 3,
    on_disk: int | None = None,
    fails_with: Exception | None = None,
) -> Album:
    """Build a real catalog Album with a locator, a snapshot, and live Parts.

    ``tracks`` sizes the creation-time manifest snapshot; ``on_disk`` sizes what a
    live ``ready_parts()`` read returns, defaulting to ``tracks``. They differ in
    life -- the background fill grows the on-disk set long after the snapshot is
    frozen -- so a test that cares about the *live* count sets them apart.

    ``fails_with`` makes the live read raise instead: a ``LookupError`` is the
    store's deleted-album contract, anything else is a real fault.
    """
    manifest = AlbumManifest(
        album_id=AlbumId(album_id),
        fmt=Format.PLAYLIST,
        tags=AlbumTags(style="techno", vibe="ambient", name=name),
        created=_EPOCH,
        fingerprint=_FINGERPRINT,
        parts=tuple(
            PartEntry(index=i, file=f"{i:03d}.mp3", status=PartStatus.READY)
            for i in range(1, tracks + 1)
        ),
    )
    locator = f"{manifest.tags.slug()}-{manifest.id.value}"
    ready = tracks if on_disk is None else on_disk
    return Album(manifest, locator, _CountingStore(manifest, ready, fails_with))


def playing_status(album: Album, index: int, of: int) -> ProgramStatus:
    """Return the radio status of ``album`` playing track ``index`` of ``of``."""
    return ProgramStatus.radio(
        ProgramName(album.locator), NowPlaying(index=index, of=of)
    )


def radio_status(index: int, of: int) -> ProgramStatus:
    """Return a multi-album radio status whose handle names no single album."""
    return ProgramStatus.radio(ProgramName("radio"), NowPlaying(index=index, of=of))


class _CountingStore(ProgramStore):
    """A ProgramStore whose one album answers ``ready_parts`` with a fixed count."""

    __slots__ = ("_fails_with", "_manifest", "_ready")

    def __init__(
        self, manifest: AlbumManifest, ready: int, fails_with: Exception | None
    ) -> None:
        self._manifest = manifest
        self._ready = ready
        self._fails_with = fails_with

    def scan(self) -> tuple[Album, ...]:
        return ()

    def open(self, directory: str) -> PartStore:
        _ = directory
        if self._fails_with is not None:
            raise self._fails_with
        return _CountingPartStore(self._manifest, self._ready)

    def create(self, draft: ManifestDraft) -> PartStore:
        raise NotImplementedError

    def delete(self, directory: str) -> None:
        return None


class _CountingPartStore(PartStore):
    """One album's Parts, live: ``ready`` of them, indexed from one."""

    __slots__ = ("_manifest", "_ready")

    def __init__(self, manifest: AlbumManifest, ready: int) -> None:
        self._manifest = manifest
        self._ready = ready

    def ready_parts(self) -> tuple[Part, ...]:
        return tuple(Part(f"part-{i}", i) for i in range(1, self._ready + 1))

    def next_index(self) -> int:
        return self._ready + 1

    def write_target(self, index: int) -> Path:
        raise NotImplementedError

    def record(self, entry: PartEntry) -> None:
        raise NotImplementedError

    def manifest(self) -> AlbumManifest:
        return self._manifest

    def prepare(self) -> None:
        return None
