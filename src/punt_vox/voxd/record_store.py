"""Daemon-owned recording store: the one place a record write can land.

A wire client never names a daemon path. It supplies at most a **bare name**
(or nothing, in which case the store content-addresses by text); the store
rejects anything absolute, separated, traversing, empty, or NUL-bearing, then
resolves the candidate under a daemon-owned root and **verifies containment**
before writing. This is the vox-zu39 (P1) security primitive: the token
authorizes audio operations, not filesystem writes as the daemon user, so no
request -- local or remote -- can escape the root.

The write itself is atomic: a fresh source is moved with an atomic rename (copy
fallback only on cross-filesystem ``EXDEV``); a cached source is copied through
the descriptor ``mkstemp`` returned (0600, ``O_EXCL``) then renamed onto the
destination with ``os.replace``, so a crash mid-write leaves no partial file
and a world-writable race cannot swap a symlink under the write.
"""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
import tempfile
from dataclasses import dataclass
from operator import attrgetter
from pathlib import Path
from typing import Self, final

from punt_vox.types import generate_filename
from punt_vox.voxd.containment import ContainmentRoot

__all__ = ["RecordStore", "RecordWrite"]

_NAME_LABEL = "recording name"


@dataclass(frozen=True, slots=True)
class RecordWrite:
    """The landed recording: its final path and byte count.

    ``byte_count`` is the size the daemon wrote, echoed to the client so the
    caller can assert the on-disk file matches (byte-correct delivery).
    """

    path: Path
    byte_count: int


@final
@dataclass(frozen=True, slots=True)
class RecordingEntry:
    """One recording in the store: its bare name and byte count (the list view)."""

    name: str
    byte_count: int


@final
class RecordStore:
    """Own the recordings root and every path decision within it.

    All naming, containment, and the atomic write live here so the containment
    invariant is enforced in exactly one place and is unit-testable without a
    socket. ``resolve`` and ``resolve_ref`` share one validator, so record
    naming and play/fetch references reject the same hostile inputs.
    """

    __slots__ = ("_root",)

    _root: Path

    def __new__(cls, root: Path) -> Self:
        self = super().__new__(cls)
        self._root = root
        return self

    @property
    def root(self) -> Path:
        """Return the recordings root every write is contained within."""
        return self._root

    def resolve(self, name: str | None, text: str) -> Path:
        """Resolve the destination for a record write, contained in the root.

        A client-supplied *name* is validated as a bare filename; only ``None``
        (absent) content-addresses by *text* -- the canonical name every other
        vox MP3 uses. An explicit empty string is an invalid name, not "absent",
        so it is rejected (``ValueError``), as are absolute, separated,
        traversing, and NUL-bearing names.
        """
        candidate = generate_filename(text) if name is None else name
        return self._resolve_within_root(candidate)

    def resolve_ref(self, ref: str) -> Path:
        """Resolve a play/fetch reference to a contained store path.

        Same validation as :meth:`resolve`: a bare name only, no path escape.
        The caller checks existence -- an unknown but well-formed name resolves
        to a path inside the root that simply does not exist yet.
        """
        return self._resolve_within_root(ref)

    def place(
        self, *, source: Path, text: str, name: str | None, cached: bool
    ) -> RecordWrite:
        """Land *source* at its contained destination atomically; return path + bytes.

        A fresh (non-cached) source is moved with an atomic rename -- no byte
        copy -- falling back to the copy path only on cross-filesystem
        ``EXDEV``. ``cached`` sources are always copied so the cache entry
        survives. Either way the destination is replaced atomically, so it is
        the complete file or untouched, never a partial write.
        """
        dest = self.resolve(name, text)
        # dest is a bare name under the root, so the parent is the root itself;
        # create it 0700 defensively (the daemon also does this at startup).
        self._root.mkdir(parents=True, exist_ok=True)
        self._root.chmod(0o700)

        if not cached:
            moved = self._move(source, dest)
            if moved is not None:
                return moved
        return self._copy(source, dest, cached=cached)

    def entries(self) -> tuple[RecordingEntry, ...]:
        """Return the store's immediate recordings (name + bytes), sorted by name.

        Lists only the files directly in the ``0700`` root -- no recursion and no
        following a symlink out of it -- so the enumeration cannot leak a path the
        client could never have named. A missing root is an empty store.
        """
        if not self._root.is_dir():
            return ()
        found = [
            RecordingEntry(child.name, child.stat().st_size)
            for child in self._root.iterdir()
            if self._is_plain_file(child)
        ]
        return tuple(sorted(found, key=attrgetter("name")))

    @staticmethod
    def _is_plain_file(child: Path) -> bool:
        """Return whether *child* is a real in-root file, not a symlink or dir."""
        return child.is_file() and not child.is_symlink()

    def remove(self, ref: str) -> None:
        """Delete one in-root recording by its bare name, or raise.

        ``ref`` runs through the shared validator (a hostile name raises
        ``ValueError`` before any filesystem touch); a well-formed but absent
        recording raises ``FileNotFoundError``, so a client can trust a success.
        """
        path = self.resolve_ref(ref)
        if not path.is_file():
            msg = f"no recording named {ref!r}"
            raise FileNotFoundError(msg)
        path.unlink()

    def _resolve_within_root(self, candidate: str) -> Path:
        """Validate a bare name and resolve it within the shared containment root."""
        return ContainmentRoot(self._root, _NAME_LABEL).resolve(candidate)

    @staticmethod
    def _move(source: Path, dest: Path) -> RecordWrite | None:
        """Atomically rename *source* onto *dest*, or None when it can't (EXDEV).

        Only a cross-filesystem rename (``EXDEV``) warrants the copy fallback;
        for any other ``OSError`` (``EACCES``, ``ENOENT``, ...) the copy path
        would not help and would mask the real cause, so re-raise it.
        """
        try:
            source.replace(dest)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                return None  # cross-device -- caller falls back to copy
            raise
        # An ephemeral source may not be private; the copy path's mkstemp temp is
        # 0600, so match that here to keep the recording private.
        dest.chmod(0o600)
        return RecordWrite(path=dest, byte_count=dest.stat().st_size)

    @staticmethod
    def _copy(source: Path, dest: Path, *, cached: bool) -> RecordWrite:
        """Copy *source* to a sibling temp then atomically rename onto *dest*.

        The byte count is taken from the temp *before* the rename (the commit
        point); the ephemeral-source cleanup afterwards is best-effort, so a
        failure past the commit never turns a completed write into a failure.
        """
        # Write THROUGH the descriptor mkstemp returned (0600, O_EXCL) -- never
        # close it and reopen the temp by name. The name could be swapped for a
        # symlink between close and reopen (TOCTOU); writing to the fd targets
        # the exact inode mkstemp created.
        fd, tmp_name = tempfile.mkstemp(dir=dest.parent, suffix=".mp3.tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as dst, source.open("rb") as src:
                shutil.copyfileobj(src, dst)
            byte_count = tmp.stat().st_size
            tmp.replace(dest)  # commit point -- the write is complete after this
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

        if not cached:
            with contextlib.suppress(OSError):
                source.unlink(missing_ok=True)
        return RecordWrite(path=dest, byte_count=byte_count)
