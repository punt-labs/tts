"""Shared factory fixtures for the music-player tests: Albums and ProgramStatus.

Exposed as fixtures (not module imports) so mypy names this ``conftest`` once --
the repo's tests reach shared builders through fixtures, never ``from ...conftest
import`` (which would give the file two module names under the ``tests`` layout).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from punt_vox.types_programs.format import Format
from punt_vox.types_programs.identifiers import ProgramName
from punt_vox.types_programs.status import ProgramStatus
from punt_vox.types_programs.status_views import NowPlaying
from punt_vox.voxd.programs.album_id import AlbumId
from punt_vox.voxd.programs.album_tags import AlbumTags, PromptFingerprint
from punt_vox.voxd.programs.catalog import Album
from punt_vox.voxd.programs.manifest import AlbumManifest, ManifestDraft, PartEntry
from punt_vox.voxd.programs.part import PartStatus
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


def make_album(album_id: str, *, name: str | None = None, tracks: int = 3) -> Album:
    """Build a real catalog Album with a locator and ``tracks`` ready parts.

    The store is never dereferenced by the view or the scene (they read only the
    id, locator, and durable tags), so a bare in-memory store suffices.
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
    return Album(manifest, locator, _NullStore())


def playing_status(album: Album, index: int, of: int) -> ProgramStatus:
    """Return the radio status of ``album`` playing track ``index`` of ``of``."""
    return ProgramStatus.radio(
        ProgramName(album.locator), NowPlaying(index=index, of=of)
    )


def radio_status(index: int, of: int) -> ProgramStatus:
    """Return a multi-album radio status whose handle names no single album."""
    return ProgramStatus.radio(ProgramName("radio"), NowPlaying(index=index, of=of))


class _NullStore(ProgramStore):
    """A ProgramStore the view/scene tests never call -- Album holds it inertly."""

    __slots__ = ()

    def scan(self) -> tuple[Album, ...]:
        return ()

    def open(self, directory: str) -> PartStore:
        raise LookupError(directory)

    def create(self, draft: ManifestDraft) -> PartStore:
        raise NotImplementedError

    def delete(self, directory: str) -> None:
        return None
