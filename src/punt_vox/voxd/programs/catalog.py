"""The in-memory album catalog -- the index over manifests every query consults.

``Catalog`` is built once at startup from ``store.scan()`` and updated on
generation; it replaces the per-list disk scan. It is the sole object list,
play, and switch consult. An :class:`Album` is a manifest paired with its
*opaque* directory locator -- never a live ``Path``; the persistence seam
dereferences the locator. Query resolution lives here: the catalog owns the
*queries* (``by_id``/``by_name``/``resume``/``newest``/``select``); the service
owns the mint *side-effect* (a domain object must not call ``store.create``).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd.programs.album_id import AlbumId
from punt_vox.voxd.programs.album_reservation import NameReservations
from punt_vox.voxd.programs.album_tags import PromptFingerprint, TagQuery
from punt_vox.voxd.programs.selection import Selection

if TYPE_CHECKING:
    from punt_vox.voxd.programs.manifest import AlbumManifest
    from punt_vox.voxd.programs.part import Part
    from punt_vox.voxd.programs.store import ProgramStore

__all__ = ["Album", "Catalog"]


@final
class Album:
    """One catalog entry: durable album metadata whose Parts are read live.

    An Album conflates nothing: its :attr:`manifest` snapshot carries only the
    *durable* metadata (id, tags, ``created``, fingerprint, format) established at
    creation and never mutated, while its Parts are a *disk read*. The background
    fill grows the on-disk manifest after the catalog registers the album, so a
    frozen parts snapshot would go stale the instant the fill writes.
    :meth:`read` and :meth:`ready_parts` therefore dereference the store live; the
    snapshot's own ``parts`` are never consulted for playback state.
    """

    __slots__ = ("_locator", "_manifest", "_store")
    _manifest: AlbumManifest
    _locator: str
    _store: ProgramStore

    def __new__(
        cls, manifest: AlbumManifest, locator: str, store: ProgramStore
    ) -> Self:
        self = super().__new__(cls)
        self._manifest = manifest
        self._locator = locator
        self._store = store
        return self

    @property
    def manifest(self) -> AlbumManifest:
        """Return the album's *durable metadata* snapshot (never its live Parts).

        Read id, tags, ``created``, fingerprint, and format here; for Parts use
        :meth:`read` or :meth:`ready_parts`, which dereference the store live.
        """
        return self._manifest

    @property
    def locator(self) -> str:
        """Return the opaque directory locator (the store dereferences it)."""
        return self._locator

    @property
    def id(self) -> AlbumId:
        """Return the album's unique id."""
        return self._manifest.id

    def read(self) -> AlbumManifest:
        """Return the manifest read live from disk -- Parts are never snapshotted."""
        return self._store.open(self._locator).manifest()

    def ready_parts(self) -> tuple[Part, ...]:
        """Return the album's ready Parts, read live from the store."""
        return self._store.open(self._locator).ready_parts()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Album):
            return NotImplemented
        return self.id == other.id and self._locator == other._locator

    def __hash__(self) -> int:
        return hash((Album, self.id, self._locator))

    def __repr__(self) -> str:
        return f"Album(id={self._manifest.id!s}, locator={self._locator!r})"


@final
class Catalog:
    """The in-memory index over album manifests -- the sole query surface."""

    __slots__ = ("_by_id", "_reservations")
    _by_id: dict[AlbumId, Album]
    # The single held-names authority both authoring paths consult: ``music new``
    # holds a curated name here across its generation await, and ``music on
    # --name`` unions these holds with the catalogued names when it mints, so
    # neither path can duplicate a name the other has claimed but not yet filed.
    _reservations: NameReservations

    def __new__(cls, albums: tuple[Album, ...]) -> Self:
        self = super().__new__(cls)
        self._by_id = {album.id: album for album in albums}
        self._reservations = NameReservations(self.taken_names)
        return self

    @property
    def reservations(self) -> NameReservations:
        """Return the shared curated-name reservation registry (``music new`` holds)."""
        return self._reservations

    def reserved_names(self) -> frozenset[str]:
        """Return every name in use: catalogued names union in-flight ``new`` holds.

        The mint-collision set ``music on --name`` auto-suffixes against, so a
        freshly bound album never takes a name a still-generating ``new`` holds.
        """
        return self.taken_names() | self._reservations.held_names()

    def add(self, album: Album) -> None:
        """Register a freshly created album so it is queryable without a re-scan."""
        self._by_id[album.id] = album

    def remove(self, album_id: AlbumId) -> None:
        """Drop the album with ``album_id`` from the index (idempotent on absence).

        The on-disk delete is the store's; this keeps the in-memory index in step
        so a removed album stops appearing in ``list`` and can never resolve for
        ``play``/``get`` afterward.
        """
        self._by_id.pop(album_id, None)

    def mint_id(self) -> AlbumId:
        """Return a fresh id absent from the catalog (delegates to AlbumId.mint)."""
        return AlbumId.mint(self._by_id.keys())

    def taken_names(self) -> frozenset[str]:
        """Return every curated album name in use (for unique-name minting)."""
        return frozenset(
            album.manifest.tags.name
            for album in self._by_id.values()
            if album.manifest.tags.name is not None
        )

    def by_id(self, album_id: AlbumId) -> Album | None:
        """Return the album with ``album_id``, or ``None`` -- a direct id lookup.

        ``None`` is the documented "no such album" contract, not a parse failure.
        """
        return self._by_id.get(album_id)

    def by_name(self, name: str) -> Album | None:
        """Return the album named ``name`` (0 or 1 -- names are unique).

        ``None`` is the documented absence contract.
        """
        for album in self._by_id.values():
            if album.manifest.tags.name == name:
                return album
        return None

    def by_tags(self, query: TagQuery) -> tuple[Album, ...]:
        """Return every album matching ``query``, newest ``created`` first."""
        matches = [
            album
            for album in self._by_id.values()
            if query.matches(album.manifest.tags)
        ]
        return tuple(sorted(matches, key=self._recency, reverse=True))

    def newest(self, query: TagQuery) -> Album | None:
        """Return the most recent album matching ``query``, or ``None``.

        ``None`` is the documented "no match" contract -- the caller mints fresh.
        """
        matches = self.by_tags(query)
        return matches[0] if matches else None

    def resume(self, query: TagQuery, fingerprint: PromptFingerprint) -> Album | None:
        """Return the newest album matching ``query`` and sharing ``fingerprint``.

        A tag match with a *different* fingerprint is a miss, so the caller mints
        a fresh album rather than filling a foreign prompt-set's pool. ``None`` is
        the documented "no resumable pool" contract.
        """
        for album in self.by_tags(query):
            if album.manifest.prompt_fingerprint == fingerprint:
                return album
        return None

    def select(self, query: TagQuery) -> Selection:
        """Return the union Selection of albums matching ``query`` (newest-first)."""
        return Selection.from_albums(
            (album.locator, album.ready_parts()) for album in self.by_tags(query)
        )

    @staticmethod
    def _recency(album: Album) -> tuple[datetime, str]:
        """Return the sort key ranking albums by ``created`` then id (deterministic)."""
        return (album.manifest.created, album.id.value)
