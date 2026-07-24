"""The music catalog authoring seam -- new/remove/manifest, distinct from playback.

``MusicLibrary`` owns the catalog *mutations* the CLI/MCP ``music`` verbs need,
kept apart from the playback-oriented :class:`ProgramService` so authoring a track
never touches the running Program. It shares the one live :class:`Catalog` and
:class:`ProgramStore` with the service, so a freshly authored album shows in
``list`` and resolves for ``play``/``get`` at once, and a removed one vanishes
from both.

The Z model (``docs/audio-programs.tex``, Catalog/System delta) governs the
mutations: :meth:`new` is ``MusicNew`` (grows the Catalog, frames ``ΞProgram``,
``ΞRadio``); a generation rejection is ``MusicNewBadPrompt`` (``ΞSystem`` -- the
Program never enters ``failed``); :meth:`remove` is ``MusicRemove`` (refuses an
album whose Parts back the active pool or selection).
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Self, final

from punt_vox.voxd.containment import ContainmentRoot
from punt_vox.voxd.programs.album_tags import AlbumTags, PromptFingerprint
from punt_vox.voxd.programs.catalog import Album
from punt_vox.voxd.programs.manifest import ManifestDraft, PartEntry
from punt_vox.voxd.programs.part import PartStatus
from punt_vox.voxd.programs.part_tags import PartTags
from punt_vox.voxd.programs.producer import (
    PartSpec,
    ProducerBadInputError,
    ProducerTransientError,
)

if TYPE_CHECKING:
    from pathlib import Path

    from punt_vox.voxd.programs.album_id import AlbumId
    from punt_vox.voxd.programs.catalog import Catalog
    from punt_vox.voxd.programs.producer import Producer
    from punt_vox.voxd.programs.store import PartStore, ProgramStore

__all__ = ["AlbumContents", "MusicLibrary", "PartFile"]

logger = logging.getLogger(__name__)

# A hand-authored ``music new`` track has no vibe/style classification; it lands
# under one neutral tag so the whole authored-track set groups together in
# ``music list`` without colliding with a generated vibe pool.
_AUTHORED_TAG: Final = "custom"
_PART_LABEL: Final = "part name"


@final
@dataclass(frozen=True, slots=True)
class PartFile:
    """One album part on disk: its bare file name and byte count (the get manifest)."""

    name: str
    byte_count: int


@final
@dataclass(frozen=True, slots=True)
class AlbumContents:
    """An album's on-disk name and its ready parts -- the ``music get`` manifest."""

    name: str
    parts: tuple[PartFile, ...]


@final
class MusicLibrary:
    """Author, describe, resolve, and remove catalog albums (never the Program)."""

    __slots__ = ("_catalog", "_producer", "_root", "_store")

    _catalog: Catalog
    _store: ProgramStore
    _root: Path
    _producer: Producer

    def __new__(
        cls, catalog: Catalog, store: ProgramStore, root: Path, producer: Producer
    ) -> Self:
        self = super().__new__(cls)
        self._catalog = catalog
        self._store = store
        self._root = root
        self._producer = producer
        return self

    async def new(self, prompt: str, name: str | None) -> AlbumId:
        """Generate one track into a fresh single-track album; return its id.

        ``MusicNew``: the id is minted fresh, the album is filed with its one
        Part, and the running Program is untouched. An empty prompt or a
        generation rejection is ``MusicNewBadPrompt`` -- it raises ``ValueError``,
        leaves the Catalog unchanged, and never fails the Program. On any
        generation failure the just-created directory is discarded, so a rejected
        authoring leaves nothing behind.
        """
        clean = prompt.strip()
        if not clean:
            raise ValueError("empty prompt")
        album_id = self._catalog.mint_id()
        draft = ManifestDraft(
            album_id=album_id,
            tags=AlbumTags(style=_AUTHORED_TAG, vibe=_AUTHORED_TAG, name=name),
            fingerprint=PromptFingerprint.from_prompts(clean, ()),
            taken_names=self._catalog.taken_names(),
        )
        store = self._store.create(draft)
        try:
            await self._generate(store, clean)
        except (ProducerBadInputError, ProducerTransientError) as exc:
            self._discard(draft.locator)
            raise ValueError(str(exc)) from exc
        self._catalog.add(Album(store.manifest(), draft.locator, self._store))
        return album_id

    def manifest(self, album_id: AlbumId) -> AlbumContents:
        """Return the on-disk album name and its ready parts, resolved by catalog id.

        The id is a catalog key (``Catalog.by_id``), never a path segment; the
        part *names* it returns are what the client fetches, each validated inside
        the resolved album directory at fetch time.
        """
        album = self._require(album_id)
        album_dir = self._root / album.locator
        parts = tuple(
            PartFile(part.identity, (album_dir / part.identity).stat().st_size)
            for part in album.ready_parts()
        )
        return AlbumContents(name=album.locator, parts=parts)

    def resolve_part(self, album_id: AlbumId, part_name: str) -> Path:
        """Resolve one album part to a contained path (catalog id + bare part name).

        Catalog-resolve the album, then bare-name-validate ``part_name`` *inside*
        the resolved album directory -- the album id is never a validated path,
        the part name always is (design F2).
        """
        album = self._require(album_id)
        album_dir = self._root / album.locator
        return ContainmentRoot(album_dir, _PART_LABEL).resolve(part_name)

    def remove(self, album_id: AlbumId, *, blocked: frozenset[str]) -> None:
        """Delete a catalog album, refusing one whose parts back the active source.

        ``MusicRemove``: refused (``ValueError``) when the album's locator is in
        ``blocked`` (the active pool/selection, D-2); otherwise its directory and
        every part are removed and the catalog forgets it.
        """
        album = self._require(album_id)
        if album.locator in blocked:
            raise ValueError(f"album {album_id.value} is playing; stop it first")
        self._store.delete(album.locator)
        self._catalog.remove(album_id)

    async def _generate(self, store: PartStore, prompt: str) -> None:
        """Produce the one Part into the album and record it ready."""
        tags = store.manifest().tags
        handle = tags.name or _AUTHORED_TAG
        target = store.write_target(1)
        spec = PartSpec(
            prompt=prompt,
            index=1,
            tags=PartTags(
                title=handle, album=handle, genre=tags.style, index=1, total=1
            ),
        )
        await self._producer.produce(spec, target)
        store.record(PartEntry(index=1, file=target.name, status=PartStatus.READY))

    def _discard(self, locator: str) -> None:
        """Best-effort remove a half-authored album directory after a failed gen."""
        with contextlib.suppress(OSError, LookupError):
            self._store.delete(locator)

    def _require(self, album_id: AlbumId) -> Album:
        """Return the catalog album for ``album_id`` or raise the clean not-found."""
        album = self._catalog.by_id(album_id)
        if album is None:
            raise ValueError(f"no album named {album_id.value!r}")
        return album
