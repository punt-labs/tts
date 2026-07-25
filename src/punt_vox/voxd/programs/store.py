"""The persistence seam: the ``ProgramStore`` and ``PartStore`` protocols.

All Program and Part persistence lives behind these two protocols (PY-IC-9,
PY-DP-11). The domain and the fill loop depend only on these interfaces, never
on ``pathlib`` or ``json`` directly, so every domain and loop test runs
filesystem-free against an in-memory fake. The production implementations live
in :mod:`punt_vox.voxd.programs.filesystem_store`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path

    from punt_vox.voxd.programs.catalog import Album
    from punt_vox.voxd.programs.manifest import AlbumManifest, ManifestDraft, PartEntry
    from punt_vox.voxd.programs.part import Part

__all__ = ["PartStore", "ProgramStore"]


@runtime_checkable
class PartStore(Protocol):
    """One album's Parts on disk, scoped to a single directory."""

    def ready_parts(self) -> tuple[Part, ...]:
        """Return the ready Parts, ordered by intrinsic index."""
        ...

    def next_index(self) -> int:
        """Return the 1-based index the next recorded Part will take."""
        ...

    def write_target(self, index: int) -> Path:
        """Return where the audio for a new Part at ``index`` should be written."""
        ...

    def record(self, entry: PartEntry) -> None:
        """Append ``entry`` to the manifest and persist it durably."""
        ...

    def manifest(self) -> AlbumManifest:
        """Return the current manifest."""
        ...

    def prepare(self) -> None:
        """Ensure the backing storage exists before a write."""
        ...


@runtime_checkable
class ProgramStore(Protocol):
    """The set of persisted albums -- the only place the programs root is read."""

    def scan(self) -> tuple[Album, ...]:
        """Return every saved album, pairing each manifest with its locator.

        The one startup disk walk that builds the catalog. Idless legacy dirs are
        skipped at this boundary, and every candidate directory is
        resolved-and-contained under the root so a symlink cannot escape.
        """
        ...

    def open(self, directory: str) -> PartStore:
        """Return the PartStore for a scan/create-validated directory, else raise.

        The ``open``-guard invariant: ``directory`` is only ever a
        locator produced by :meth:`scan` or :meth:`create`, so no wire/CLI path
        can hand this a directory that resolves outside the root.
        """
        ...

    def create(self, draft: ManifestDraft) -> PartStore:
        """Materialise ``draft`` into a fresh album directory and return its store.

        The store owns the clock: it stamps ``created = now(UTC)``.
        """
        ...

    def delete(self, directory: str) -> None:
        """Delete a scan/create-validated album directory and its Parts.

        Same ``open``-guard invariant as :meth:`open`: ``directory`` is only ever
        a locator produced by :meth:`scan` or :meth:`create`, so no wire/CLI path
        can hand this a directory that resolves outside the root.

        Idempotent: deleting an already-missing directory is a no-op, not an
        error, so a caller can always forget its catalog entry without a stale
        directory leaving a ghost id. A containment or validation failure on the
        locator still raises.
        """
        ...
