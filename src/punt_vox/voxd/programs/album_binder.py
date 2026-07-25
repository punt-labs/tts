"""Resolve which album a turn-on binds: named resume, tag+fingerprint resume, or mint.

``AlbumBinder`` owns album *resolution* -- the one algorithm the daemon runs
before it seeds a Program: a ``--name`` resumes the named album (or mints an
auto-suffixed one on a fresh name); otherwise the newest album matching the
``(style, vibe)`` tags *and* the incoming prompt fingerprint resumes, and a
fingerprint mismatch mints a fresh album rather than growing a foreign pool. It
holds the one catalog and store the service holds, so a mint it registers is
visible to every later resolution.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd.programs.album_tags import AlbumTags, TagQuery
from punt_vox.voxd.programs.catalog import Album
from punt_vox.voxd.programs.manifest import ManifestDraft

if TYPE_CHECKING:
    from punt_vox.voxd.programs.album_tags import PromptFingerprint
    from punt_vox.voxd.programs.catalog import Catalog
    from punt_vox.voxd.programs.store import ProgramStore

__all__ = ["AlbumBinder"]


@final
class AlbumBinder:
    """Resolve the album a turn-on binds, minting a fresh one when none is safe."""

    __slots__ = ("_catalog", "_store")
    _catalog: Catalog
    _store: ProgramStore

    def __new__(cls, catalog: Catalog, store: ProgramStore) -> Self:
        self = super().__new__(cls)
        self._catalog = catalog
        self._store = store
        return self

    def bind(
        self, style: str, vibe: str, name: str | None, fingerprint: PromptFingerprint
    ) -> Album:
        """Resolve the album to bind: named resume, tag+fingerprint resume, or mint."""
        handle = (name or "").strip()
        if handle:
            existing = self._catalog.by_name(handle)
            if existing is not None and self._safe_to_resume(existing, fingerprint):
                return existing
            return self._mint(style, vibe, handle, fingerprint)
        resumed = self._catalog.resume(TagQuery(style=style, vibe=vibe), fingerprint)
        if resumed is not None:
            return resumed
        return self._mint(style, vibe, None, fingerprint)

    def _safe_to_resume(self, album: Album, fingerprint: PromptFingerprint) -> bool:
        """Return whether resuming ``album`` cannot blend two prompt sets in one pool.

        A named resume attaches the *incoming* prompt set to the album's continued
        fill. Generating a partly-filled album's remaining tracks from a prompt set
        other than the one that authored it would mix two identities in one pool,
        so a partial album resumes only when the incoming fingerprint matches its
        own. A full album never fills, so any prompt set is safe. On a mismatch the
        caller mints a fresh, auto-suffixed album instead of filling foreign prompts.
        """
        if album.manifest.prompt_fingerprint == fingerprint:
            return True
        return self._is_full(album)

    def _is_full(self, album: Album) -> bool:
        """Return whether ``album`` already holds a full pool for its format."""
        ready = self._store.open(album.locator).ready_parts()
        return len(ready) >= album.manifest.format.pool_size

    def _mint(
        self, style: str, vibe: str, name: str | None, fingerprint: PromptFingerprint
    ) -> Album:
        """Create a fresh album, register it, suffixing around reserved names."""
        taken = self._catalog.reserved_names()
        final_name = None if name is None else AlbumTags.mint_unique_name(name, taken)
        tags = AlbumTags(style=style, vibe=vibe, name=final_name)
        draft = ManifestDraft(
            album_id=self._catalog.mint_id(),
            tags=tags,
            fingerprint=fingerprint,
            taken_names=taken,
        )
        store = self._store.create(draft)
        album = Album(store.manifest(), draft.locator, self._store)
        self._catalog.add(album)
        return album
