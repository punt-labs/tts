"""The one structural gate for a client-supplied bare filename.

A *bare name* is a single filesystem segment -- a recording id, an album name,
or a part name -- that an untrusted client supplies for the daemon (or the
client itself, when it writes a fetched album) to join onto a trusted directory.
:class:`BareName` is a value object whose construction *is* the validation: it
rejects anything empty, NUL-bearing, absolute, separator-bearing, traversing, or
non-printable before the name can be held, so an illegal segment can never exist
as a ``BareName``.

Every seam that joins an untrusted segment onto a trusted root builds a
``BareName`` first -- the daemon's :class:`~punt_vox.voxd.containment.ContainmentRoot`
and the client's ``music get`` writer alike -- so a name is judged by identical
rules on both sides of the wire. This module is stdlib-only, so the lightweight
client layer can import it without reaching into daemon code.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Final, Self, final

__all__ = ["BareName"]

# Names that name the directory itself rather than a file in it.
_DIR_TOKENS: Final = frozenset({".", ".."})

# Structural rejections, first-match-raises, cheapest-first. ``not isprintable``
# rejects an embedded newline, tab, or terminal escape a name would echo raw
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
class BareName:
    """A single filesystem segment, structurally validated on construction.

    ``label`` names what the client supplied (``"recording name"``, ``"album
    name"``, ``"part name"``) so a rejection reads in the caller's own vocabulary
    while the structural rules stay identical across every seam.
    """

    __slots__ = ("_value",)

    _value: str

    def __new__(cls, name: str, label: str) -> Self:
        for is_rejected, suffix in _REJECTIONS:
            if is_rejected(name):
                raise ValueError(f"{label} {suffix}")
        self = super().__new__(cls)
        self._value = name
        return self

    @property
    def value(self) -> str:
        """Return the validated segment -- safe to join onto a trusted root."""
        return self._value

    def __fspath__(self) -> str:
        """Return the segment for ``os.PathLike`` joins (``root / bare_name``)."""
        return self._value
