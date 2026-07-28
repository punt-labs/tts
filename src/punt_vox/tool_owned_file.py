"""A file under ``.punt-labs/vox/`` that vox writes without following a symlink."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Self, final

__all__ = ["ToolOwnedFile"]

# ``O_NOFOLLOW`` makes the open fail (``ELOOP``) when the final path component is
# a symlink, closing the window between the ``is_symlink`` check and the open. An
# untrusted repo could plant ``.punt-labs/vox/enabled`` or the deposited
# ``CLAUDE.md`` guide as a symlink to a sensitive path (``~/.ssh/id_rsa``,
# credentials); without this the write would clobber the *link target*.
# ``O_TRUNC`` reproduces ``write_text``'s wholesale-overwrite semantics.
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
_NEW_FILE_MODE = 0o644


@final
class ToolOwnedFile:
    """A tool-owned path vox writes only when it is not a symlink.

    Both the ``enabled`` marker and the deposited guide live inside vox's own
    ``.punt-labs/vox/`` directory, so vox -- never the user -- owns their bytes.
    A symlink at either path is therefore never legitimate: it can only be an
    untrusted repo trying to redirect vox's write onto a file it should not
    touch. Every write refuses such a path rather than following it.
    """

    __slots__ = ("_base", "_path")

    _path: Path
    _base: Path

    def __new__(cls, path: Path, base: Path) -> Self:
        self = super().__new__(cls)
        self._path = path
        self._base = base
        return self

    @property
    def path(self) -> Path:
        """Return the managed path."""
        return self._path

    def is_present(self) -> bool:
        """Return whether the path is a regular file (a symlink counts as absent)."""
        return self._path.is_file() and not self._path.is_symlink()

    def write(self, text: str) -> None:
        """Write *text* wholesale, making the parent dir, refusing a symlink.

        The whole path from the trusted *base* to the leaf is checked for symlinks
        **before** any directory is created, then the open itself carries
        ``O_NOFOLLOW`` so a symlink planted at the leaf in the race between the
        check and the open still fails loud instead of overwriting its target.
        """
        self._reject_symlinked_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, _WRITE_FLAGS, _NEW_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)

    def remove(self) -> None:
        """Delete the path; an already-absent path is a clean no-op.

        ``unlink`` removes the link itself, never its target, so no symlink guard
        is needed here -- a planted symlink is destroyed, not followed.
        """
        self._path.unlink(missing_ok=True)

    def _reject_symlinked_path(self) -> None:
        """Raise ``ValueError`` if any component from *base* to the leaf is a symlink.

        ``O_NOFOLLOW`` guards only the final component, and it fires only at open
        time -- after ``mkdir`` has already run. An untrusted repo that plants an
        *ancestor* (``.punt-labs`` -> outside the repo) would otherwise have the
        ``mkdir`` create directories, and the write land, in the symlink's target.
        Walk every component below the trusted real *base* down to the leaf and
        refuse -- before any directory is created -- if one is a symlink.
        """
        relative = self._path.relative_to(self._base)
        ancestor = self._base
        for part in relative.parts[:-1]:
            ancestor = ancestor / part
            if ancestor.is_symlink():
                msg = (
                    f"refusing to write {self._path}: a symlinked ancestor "
                    f"{ancestor} would redirect the write outside the repo"
                )
                raise ValueError(msg)
        self._reject_symlink()

    def _reject_symlink(self) -> None:
        """Raise ``ValueError`` if a symlink sits at the tool-owned leaf path."""
        if self._path.is_symlink():
            msg = (
                f"refusing to write {self._path}: a symlink at a tool-owned path "
                "would redirect the write to its target"
            )
            raise ValueError(msg)
