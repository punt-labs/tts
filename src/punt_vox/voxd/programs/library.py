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
from typing import TYPE_CHECKING, Final, Self, final

from punt_vox.voxd.containment import ContainmentRoot
from punt_vox.voxd.programs.album_contents import AlbumContents
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
    from punt_vox.voxd.programs.album_reservation import AlbumReservation
    from punt_vox.voxd.programs.catalog import Catalog
    from punt_vox.voxd.programs.producer import Producer
    from punt_vox.voxd.programs.store import PartStore, ProgramStore

__all__ = ["MusicLibrary"]

logger = logging.getLogger(__name__)

# A hand-authored ``music new`` track has no vibe/style classification; it lands
# under one neutral tag so the whole authored-track set groups together in
# ``music list`` without colliding with a generated vibe pool.
_AUTHORED_TAG: Final = "custom"
_PART_LABEL: Final = "part name"


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

    def reserve(self, prompt: str, name: str | None) -> AlbumReservation:
        """Validate the prompt and reserve the curated name before any generation.

        All pre-generation input rejection is synchronous and happens here: an
        empty prompt, or a curated name already taken or held by another in-flight
        ``new``, raises ``ValueError`` (``MusicNewBadPrompt``). A wire caller runs
        this *before* acking, so a malformed or duplicate request never receives a
        ``generating`` ack it will only fail after. The returned reservation holds
        the name until its context exits (see :meth:`produce`).
        """
        clean = prompt.strip()
        if not clean:
            raise ValueError("empty prompt")
        tags = AlbumTags(style=_AUTHORED_TAG, vibe=_AUTHORED_TAG, name=name)
        return self._catalog.reservations.hold(clean, tags)

    async def produce(self, reservation: AlbumReservation) -> AlbumId:
        """Generate the reserved album's one track and catalog it; return its id.

        ``MusicNew`` proper: mint the id, file the album with its one Part, leave
        the running Program untouched. The prompt and curated name were already
        validated and held by :meth:`reserve`; the caller owns releasing the
        reservation (its context manager) on every path, so a rejection here
        (``MusicNewBadPrompt``) frees the name and leaves the Catalog unchanged.
        """
        draft = ManifestDraft(
            album_id=self._catalog.mint_id(),
            tags=reservation.tags,
            fingerprint=PromptFingerprint.from_prompts(reservation.prompt, ()),
            taken_names=self._catalog.taken_names(),
        )
        store = await self._materialise(draft, reservation.prompt)
        self._catalog.add(Album(store.manifest(), draft.locator, self._store))
        return draft.album_id

    async def new(self, prompt: str, name: str | None) -> AlbumId:
        """Author one track into a fresh album in one call: reserve, generate, file.

        A convenience over :meth:`reserve` + :meth:`produce` for callers that do
        not interpose work -- a wire ``generating`` ack -- between reserving the
        name and generating. Rejection semantics are identical: an empty prompt, a
        duplicate curated name, or a generation rejection raises ``ValueError`` and
        leaves the Catalog unchanged, and the held name is always released.
        """
        with self.reserve(prompt, name) as reservation:
            return await self.produce(reservation)

    def manifest(self, album_id: AlbumId) -> AlbumContents:
        """Return the album's on-disk name and ready parts, resolved by catalog id.

        The id is a catalog key, never a path; the per-part containment (a corrupt
        manifest entry can never stat outside the album dir) lives on ``AlbumContents``.
        """
        album = self._require(album_id)
        root = ContainmentRoot(self._root / album.locator, _PART_LABEL)
        return AlbumContents.from_album(album, root)

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

    async def _materialise(self, draft: ManifestDraft, prompt: str) -> PartStore:
        """Create the album directory and generate its Part, returning the store.

        A failure *after* this call created the directory discards it so no orphan
        is left. A locator collision (``FileExistsError`` from
        ``mkdir(exist_ok=False)``) means the directory already existed -- this
        call did NOT create it, so the pre-existing album is left intact and the
        collision is surfaced as a ``ValueError``. A provider rejection becomes a
        ``ValueError``; any other post-create ``OSError`` re-raises after discard.
        """
        try:
            store = self._store.create(draft)
            await self._generate(store, prompt)
        except FileExistsError as exc:
            msg = f"album directory {draft.locator!r} already exists"
            raise ValueError(msg) from exc
        except (ProducerBadInputError, ProducerTransientError) as exc:
            self._discard(draft.locator)
            raise ValueError(str(exc)) from exc
        except OSError:
            self._discard(draft.locator)
            raise
        return store

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
