"""Resolve a client-supplied bare name within a daemon-owned root.

A wire client never names a daemon path. It supplies at most a **bare name** --
a recording id, or the part name inside a resolved album directory -- and the
daemon owns every path decision. :class:`ContainmentRoot` turns such a name into
a contained :class:`~pathlib.Path`: it validates the name through the shared
:class:`~punt_vox.bare_name.BareName` gate (rejecting anything absolute,
separator-bearing, traversing, empty, NUL-bearing, or non-printable) *before*
touching the filesystem, then resolves the candidate under its root and verifies
``is_relative_to`` *after* ``.resolve()`` so no symlink or normalization can
escape. This is the vox-zu39 (P1) security primitive shared by recording naming,
play/fetch references, and music-part fetch, so the containment invariant is
enforced in exactly one place and unit-testable without a socket.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self, final

from punt_vox.bare_name import BareName
from punt_vox.voxd.path_status import PathStatus

__all__ = ["ContainmentRoot"]


@final
class ContainmentRoot:
    """Own a daemon root and validate every bare name resolved beneath it.

    ``label`` names what the client supplies (``"recording name"``,
    ``"part name"``) so a rejection reads in the caller's own vocabulary while the
    structural rules stay identical across every store.
    """

    __slots__ = ("_label", "_root")

    _root: Path
    _label: str

    def __new__(cls, root: Path, label: str) -> Self:
        self = super().__new__(cls)
        self._root = root
        self._label = label
        return self

    @property
    def root(self) -> Path:
        """Return the root every resolved name is contained within."""
        return self._root

    def resolve(self, name: str) -> Path:
        """Return ``name`` resolved under the root, or raise ``ValueError``.

        Structural rejections run before any filesystem touch; then a
        post-``resolve`` ``is_relative_to`` check catches any symlink or
        normalization that escaped the root. Every rejection raises ``ValueError``
        with a lowercase message the handler turns into a one-line error frame.

        This *follows* symlinks (``.resolve()``), so it is the reference for a
        read/write that must reach the file a name points at. To act on the entry
        itself without following it out of the store, use :meth:`contained_child`.
        """
        safe = BareName(name, self._label).value
        resolved = (self._root / safe).resolve()
        if not resolved.is_relative_to(self._root.resolve()):
            raise ValueError(f"{self._label} escapes its root")
        return resolved

    def contained_child(self, name: str) -> Path:
        """Return ``root / name`` as an immediate child, without following it.

        Runs the same :class:`BareName` gate as :meth:`resolve` but never calls
        ``.resolve()``, so a symlink entry is returned as the link itself, not the
        file it points at. The gate forbids separators and traversal, so
        ``root / name`` is always a direct child of the root -- there is no path
        left to escape, hence no post-resolve containment check. Unlinking such a
        child removes the symlink, never its target, so a delete can never reach a
        file outside the store.
        """
        return self._root / BareName(name, self._label).value

    def contained_regular_file(self, name: str) -> Path:
        """Return the direct child ``name`` only if it is a regular file, unfollowed.

        The read counterpart of :meth:`contained_child`: it takes the same
        bare-name gate, then requires the child be a regular file via a no-follow
        stat. A symlink (even one pointing at an in-root file), a directory, or a
        well-formed but absent name raises ``ValueError`` rather than being
        followed to whatever it targets -- the no-symlink read invariant a
        fetch/measure shares, so an entry is served for itself and never a link's
        target. Any other stat ``OSError`` (a permission/device fault) propagates.
        """
        child = self.contained_child(name)
        if not PathStatus.of(child, follow_symlinks=False).is_regular_file:
            raise ValueError(f"no {self._label} {name!r}")
        return child
