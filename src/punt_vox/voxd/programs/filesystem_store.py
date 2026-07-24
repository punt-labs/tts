"""Filesystem implementations of the album persistence protocols.

These are the *only* modules that touch the disk for album data: all ``pathlib``
access, directory globbing, and atomic manifest writes live here. Each album is a
``<slug>-<id>`` directory under a shared root, holding a ``manifest.json`` (UTF-8)
and its ``NNN.mp3`` Part files. Manifest writes are atomic and fsynced (temp file
+ ``os.replace``) so a crash mid-write never leaves a torn manifest. This is the
seam that dereferences an opaque locator to a ``Path``.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Self, final

from punt_vox.atomic_file import AtomicFile
from punt_vox.types_programs.identifiers import ProgramName
from punt_vox.types_programs.wire import JsonObject
from punt_vox.voxd.programs.catalog import Album
from punt_vox.voxd.programs.manifest import AlbumManifest, ManifestDraft, PartEntry
from punt_vox.voxd.programs.part import Part

__all__ = ["FilesystemPartStore", "FilesystemProgramStore"]

logger = logging.getLogger(__name__)

_MANIFEST_NAME = "manifest.json"
# A manifest is small JSON (album tags + up to a pool of Part entries); anything
# past this ceiling is corrupt or a planted file, and is rejected before the read
# so one giant manifest cannot exhaust daemon memory at scan time.
_MAX_MANIFEST_BYTES = 1024 * 1024


@final
class FilesystemPartStore:
    """One album directory: its ``manifest.json`` and ``NNN.mp3`` Part files."""

    __slots__ = ("_directory", "_manifest")
    _directory: Path
    _manifest: AlbumManifest

    def __new__(cls, directory: Path, manifest: AlbumManifest) -> Self:
        self = super().__new__(cls)
        self._directory = directory
        self._manifest = manifest
        return self

    @property
    def directory(self) -> Path:
        """Return the album directory this store backs."""
        return self._directory

    def ready_parts(self) -> tuple[Part, ...]:
        """Return the ready Parts, ordered by intrinsic index."""
        return self._manifest.ready_parts()

    def next_index(self) -> int:
        """Return the 1-based index the next recorded Part will take."""
        return self._manifest.next_index()

    def write_target(self, index: int) -> Path:
        """Return the ``NNN.mp3`` audio path for a new Part at ``index``."""
        return self._directory / f"{index:03d}.mp3"

    def manifest(self) -> AlbumManifest:
        """Return the current manifest."""
        return self._manifest

    def prepare(self) -> None:
        """Ensure the album directory exists before a write."""
        self._directory.mkdir(parents=True, exist_ok=True)

    def record(self, entry: PartEntry) -> None:
        """Append ``entry`` to the manifest and persist it durably."""
        self._manifest = self._manifest.with_part(entry)
        self.save_manifest()

    def save_manifest(self) -> None:
        """Write the current manifest atomically via :class:`AtomicFile`."""
        self.prepare()
        AtomicFile(self._directory / _MANIFEST_NAME).replace(self._manifest.to_json())


@final
class FilesystemProgramStore:
    """The set of albums under one root directory (``~/Music/vox``)."""

    __slots__ = ("_root",)
    _root: Path

    def __new__(cls, root: Path) -> Self:
        self = super().__new__(cls)
        self._root = root
        return self

    @property
    def root(self) -> Path:
        """Return the programs root directory."""
        return self._root

    def scan(self) -> tuple[Album, ...]:
        """Return every saved album, in per-album isolation.

        The one startup disk walk. Each ``*/manifest.json`` is scanned alone, so a
        single escaping, idless-legacy, or corrupt directory is skipped without
        aborting the walk -- one torn file can never brick the catalog or daemon.
        """
        if not self._root.is_dir():
            return ()
        albums = [
            album
            for path in sorted(self._root.glob(f"*/{_MANIFEST_NAME}"))
            if (album := self._scan_one(path)) is not None
        ]
        return tuple(albums)

    def open(self, directory: str) -> FilesystemPartStore:
        """Return the PartStore for a scan/create-validated directory, else raise."""
        path = self._contained_dir(directory)
        manifest_path = path / _MANIFEST_NAME
        if not manifest_path.is_file():
            msg = f"no saved album at directory {directory!r}"
            raise LookupError(msg)
        manifest = AlbumManifest.from_json(self._read_manifest_text(manifest_path))
        return FilesystemPartStore(path, manifest)

    def create(self, draft: ManifestDraft) -> FilesystemPartStore:
        """Materialise ``draft`` into a fresh ``<slug>-<id>`` directory.

        The name is a single validated segment (``ProgramName``), so it cannot
        traverse; ``mkdir(exist_ok=False)`` is the second-line race guard behind
        ``AlbumId.mint``. The store owns the clock -- it stamps now.
        """
        segment = ProgramName(draft.locator)
        directory = self._contained_dir(segment.value)
        directory.mkdir(parents=True, exist_ok=False)
        store = FilesystemPartStore(directory, draft.stamped(datetime.now(UTC)))
        store.save_manifest()
        return store

    def delete(self, directory: str) -> None:
        """Remove a scan/create-validated album directory and every Part within it.

        ``directory`` is a locator produced by :meth:`scan`/:meth:`create`, so the
        single-segment + containment guard of :meth:`_contained_dir` reconfirms it
        cannot escape the root before the recursive unlink.
        """
        path = self._contained_dir(directory)
        shutil.rmtree(path)

    def _scan_one(self, manifest_path: Path) -> Album | None:
        """Return the Album for one manifest, or ``None`` to skip the directory.

        Three skips, two log levels: an escaping dir and an idless legacy
        record are intentional debug-logged skips; a corrupt id-bearing manifest
        is a real fault -- caught here and logged at ERROR with its directory
        rather than propagated out of ``scan`` to brick the whole daemon.
        """
        directory = manifest_path.parent
        if not self._is_contained(directory):
            logger.debug("skipping album dir outside root: %s", directory)
            return None
        try:
            obj = JsonObject.parse(self._read_manifest_text(manifest_path), "manifest")
            if obj.opt_str("id") is None:
                logger.debug("skipping idless legacy album dir: %s", directory)
                return None
            return Album(AlbumManifest.from_wire(obj), directory.name, self)
        except (ValueError, OSError) as exc:
            logger.error("skipping corrupt manifest in %s: %s", directory.name, exc)
            return None

    @staticmethod
    def _read_manifest_text(manifest_path: Path) -> str:
        """Return a manifest's UTF-8 text, refusing a symlink or an oversized file.

        The manifest is opened ``O_NOFOLLOW`` so a symlinked ``manifest.json`` (a
        planted file pointing at an arbitrary target) raises ``OSError`` instead
        of being read through. ``fstat`` on that same descriptor enforces the
        ceiling before any bytes are pulled into memory, so an oversized (corrupt
        or planted) manifest raises ``ValueError``. ``_scan_one`` catches both and
        skips the album; ``open`` lets them surface as the corrupt-album error.
        """
        fd = os.open(manifest_path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding="utf-8") as handle:
            if os.fstat(fd).st_size > _MAX_MANIFEST_BYTES:
                name = manifest_path.name
                msg = f"manifest exceeds {_MAX_MANIFEST_BYTES} bytes: {name}"
                raise ValueError(msg)
            return handle.read()

    def _contained_dir(self, directory: str) -> Path:
        """Return ``root/directory`` for a single validated segment, else raise.

        A locator is always one plain directory-name segment produced by ``scan``
        or ``create``, never a path. Rejecting anything that is not *already* its
        own sole canonical component before the containment check restores that
        single-segment invariant (defense in depth): empty, ``.``, ``..``, ``a/b``,
        and non-canonical spellings like ``./foo`` or ``foo/`` are all refused,
        rather than silently normalized to ``foo`` and resolved under the root.
        """
        parts = Path(directory).parts
        if len(parts) != 1 or parts[0] in (".", "..") or directory != parts[0]:
            msg = f"album locator must be a single path segment: {directory!r}"
            raise ValueError(msg)
        path = self._root / directory
        if not self._is_contained(path):
            msg = f"album directory escapes the programs root: {directory!r}"
            raise ValueError(msg)
        return path

    def _is_contained(self, directory: Path) -> bool:
        """Return whether ``directory`` resolves to a path under the root."""
        root = self._root.resolve()
        resolved = directory.resolve()
        return resolved == root or root in resolved.parents
