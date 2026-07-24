"""Validate a client-supplied bare name and resolve it within a daemon-owned root.

A wire client never names a daemon path. It supplies at most a **bare name** --
a recording id, or the part name inside a resolved album directory -- and the
daemon owns every path decision. :class:`ContainmentRoot` is the single validator
that turns such a name into a contained :class:`~pathlib.Path`: it rejects
anything absolute, separator-bearing, traversing, empty, NUL-bearing, or
non-printable *before* touching the filesystem, then resolves the candidate under
its root and verifies ``is_relative_to`` *after* ``.resolve()`` so no symlink or
normalization can escape. This is the vox-zu39 (P1) security primitive shared by
recording naming, play/fetch references, and music-part fetch, so the containment
invariant is enforced in exactly one place and unit-testable without a socket.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Final, Self, final

__all__ = ["ContainmentRoot"]

# Names that name the directory itself rather than a file in it.
_DIR_TOKENS: Final = frozenset({".", ".."})

# Structural rejections, first-match-raises, cheapest-first. ``not isprintable``
# rejects an embedded newline, tab, or terminal escape a locator would echo raw
# into the operator's log or terminal -- a log/terminal-injection vector.
_REJECTIONS: Final[tuple[tuple[Callable[[str], bool], str], ...]] = (
    (lambda c: not c, "must not be empty"),
    (lambda c: "\x00" in c, "must not contain a NUL byte"),
    (lambda c: Path(c).is_absolute(), "must not be absolute"),
    (lambda c: "/" in c or "\\" in c, "must not contain a path separator"),
    (lambda c: c in _DIR_TOKENS, "must be a filename, not '.' or '..'"),
    (lambda c: not c.isprintable(), "must not contain a non-printable character"),
)


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
        """
        for is_rejected, suffix in _REJECTIONS:
            if is_rejected(name):
                raise ValueError(f"{self._label} {suffix}")

        resolved = (self._root / name).resolve()
        if not resolved.is_relative_to(self._root.resolve()):
            raise ValueError(f"{self._label} escapes its root")
        return resolved
