"""Classify a store path once: an existing kind, a benign absence, or a fault.

A boolean ``Path`` predicate (``is_dir``/``is_file``/``is_symlink``) answers
False on *any* ``OSError``, so it cannot tell "the path is absent" from "the path
exists but cannot be accessed" -- a ``PermissionError`` on a store root reads as
a benign empty listing instead of the operational failure it is.

:class:`PathStatus` draws that line. Its :meth:`of` factory stats the path and
returns a status whose predicates classify it; a missing path (``ENOENT``) yields
an absent status whose predicates are all False, but any *other* ``OSError`` --
``EACCES``, ``ELOOP``, ``EIO`` -- propagates, so a store that exists but cannot be
read surfaces the fault to its handler's guard rather than masquerading as empty.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Self, final

__all__ = ["PathStatus"]

_ABSENT_MODE = 0


@final
class PathStatus:
    """The ``st_mode`` of a stat'd path, or ``0`` when the path is absent.

    Constructed through :meth:`of`, never directly with a raw mode by callers:
    the factory owns the stat-and-branch that distinguishes absence from an
    access fault. The predicates read from a single stat, so a name and its kind
    come from one syscall -- there is no second stat to race.
    """

    __slots__ = ("_mode",)

    _mode: int

    def __new__(cls, mode: int) -> Self:
        self = super().__new__(cls)
        self._mode = mode
        return self

    @classmethod
    def of(cls, path: Path, *, follow_symlinks: bool = True) -> Self:
        """Return the status of *path*, or an absent status when it does not exist.

        ``ENOENT`` is the one benign miss -- the store (or entry) simply does not
        exist yet -- and yields an absent status. Every other ``OSError`` is an
        operational failure and propagates unchanged, so a path that exists but
        cannot be stat'd never collapses into a false "absent". Pass
        ``follow_symlinks=False`` to classify a symlink as the link itself rather
        than the file it points at.
        """
        try:
            info = path.stat(follow_symlinks=follow_symlinks)
        except FileNotFoundError:
            return cls(_ABSENT_MODE)
        return cls(info.st_mode)

    @property
    def is_directory(self) -> bool:
        """Return whether the path is an existing directory."""
        return stat.S_ISDIR(self._mode)

    @property
    def is_regular_file(self) -> bool:
        """Return whether the path is an existing regular file."""
        return stat.S_ISREG(self._mode)

    @property
    def is_symlink(self) -> bool:
        """Return whether the path is a symlink (only when stat'd no-follow)."""
        return stat.S_ISLNK(self._mode)
