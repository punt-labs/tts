"""Tests for AlbumNames: the catalogue-wide friendly-name map and its inverse.

Pins the two properties click-to-play depends on: a friendly name is unique across
the catalog (so a clicked cell names exactly one album), and the map inverts (so the
receive leg resolves that cell back to its album). The collision case is the one the
original slug guarded -- two same-``(style, vibe)`` pools minted minutes apart title
identically once the timestamp is dropped, so each must gain its id to stay unique.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from punt_vox.types_programs.format import Format
from punt_vox.voxd.music_player.album_names import AlbumNames
from punt_vox.voxd.programs.album_id import AlbumId
from punt_vox.voxd.programs.album_tags import AlbumTags, PromptFingerprint
from punt_vox.voxd.programs.catalog import Album
from punt_vox.voxd.programs.manifest import AlbumManifest, ManifestDraft, PartEntry
from punt_vox.voxd.programs.part import PartStatus
from punt_vox.voxd.programs.store import PartStore, ProgramStore

_FINGERPRINT = PromptFingerprint.from_prompts("base", ())
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


class _NullStore(ProgramStore):
    """A ProgramStore these tests never dereference -- Album holds it inertly."""

    __slots__ = ()

    def scan(self) -> tuple[Album, ...]:
        return ()

    def open(self, directory: str) -> PartStore:
        raise LookupError(directory)

    def create(self, draft: ManifestDraft) -> PartStore:
        raise NotImplementedError

    def delete(self, directory: str) -> None:
        return None


def _album(album_id: str, *, style: str, vibe: str, name: str | None) -> Album:
    """Build a catalog Album carrying explicit ``(style, vibe, name)`` tags."""
    manifest = AlbumManifest(
        album_id=AlbumId(album_id),
        fmt=Format.PLAYLIST,
        tags=AlbumTags(style=style, vibe=vibe, name=name),
        created=_EPOCH,
        fingerprint=_FINGERPRINT,
        parts=(PartEntry(index=1, file="001.mp3", status=PartStatus.READY),),
    )
    return Album(manifest, f"{manifest.tags.slug()}-{album_id}", _NullStore())


def test_curated_name_titles_verbatim() -> None:
    album = _album("aa11bb", style="techno", vibe="ambient", name="Techno Mix")
    assert AlbumNames((album,)).friendly(album) == "Techno Mix"


def test_auto_name_drops_the_timestamp_and_title_cases() -> None:
    album = _album(
        "aa11bb", style="synthwave", vibe="synthwave", name="synthwave-20260726-0326"
    )
    assert AlbumNames((album,)).friendly(album) == "Synthwave"


def test_unnamed_album_titles_as_album() -> None:
    album = _album("aa11bb", style="techno", vibe="ambient", name=None)
    assert AlbumNames((album,)).friendly(album) == "Album"


def test_colliding_bases_are_disambiguated_by_id() -> None:
    # Two k-pop pools minted a minute apart title identically once the stamp drops;
    # each gains its id so the two cells stay distinct and resolve to the right album.
    first = _album("aa11bb", style="k-pop", vibe="k-pop", name="k-pop-20260726-0326")
    second = _album("cc22dd", style="k-pop", vibe="k-pop", name="k-pop-20260726-03261")
    names = AlbumNames((first, second))

    assert names.friendly(first) == "K Pop (aa11bb)"
    assert names.friendly(second) == "K Pop (cc22dd)"
    assert names.friendly(first) != names.friendly(second)


def test_resolve_inverts_a_colliding_cell_to_its_own_album() -> None:
    first = _album("aa11bb", style="k-pop", vibe="k-pop", name="k-pop-20260726-0326")
    second = _album("cc22dd", style="k-pop", vibe="k-pop", name="k-pop-20260726-03261")
    names = AlbumNames((first, second))

    assert names.resolve("K Pop (aa11bb)") is first
    assert names.resolve("K Pop (cc22dd)") is second


def test_resolve_inverts_a_plain_name_to_its_album() -> None:
    album = _album("aa11bb", style="techno", vibe="ambient", name="Techno Mix")
    names = AlbumNames((album,))

    assert names.resolve("Techno Mix") is album


def test_resolve_raises_when_no_album_matches() -> None:
    album = _album("aa11bb", style="techno", vibe="ambient", name="Techno Mix")
    with pytest.raises(ValueError, match="names no catalogued album"):
        AlbumNames((album,)).resolve("Ghost Album")
