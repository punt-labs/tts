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

    __slots__ = ("_path",)

    _path: Path

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        self._path = path
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

        The path is checked with :meth:`Path.is_symlink` for a clear error, and
        the open itself carries ``O_NOFOLLOW`` so a symlink planted in the race
        between the check and the open still fails loud instead of overwriting
        the link's target.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._reject_symlink()
        fd = os.open(self._path, _WRITE_FLAGS, _NEW_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)

    def remove(self) -> None:
        """Delete the path; an already-absent path is a clean no-op.

        ``unlink`` removes the link itself, never its target, so no symlink guard
        is needed here -- a planted symlink is destroyed, not followed.
        """
        self._path.unlink(missing_ok=True)

    def _reject_symlink(self) -> None:
        """Raise ``ValueError`` if a symlink sits at the tool-owned path."""
        if self._path.is_symlink():
            msg = (
                f"refusing to write {self._path}: a symlink at a tool-owned path "
                "would redirect the write to its target"
            )
            raise ValueError(msg)
