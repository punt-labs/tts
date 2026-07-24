"""The ``music get`` manifest value objects: one album's name and its ready parts.

:class:`AlbumContents` is what a ``music get`` reply carries -- the album's on-disk
name and a :class:`PartFile` per ready part. Each ``PartFile`` measures itself
from the store: its bare name comes from the on-disk manifest, so it is
bare-name-validated inside the album directory (:meth:`ContainmentRoot.contained_child`)
*before* it is stat'd -- a corrupt or hostile manifest entry bearing a separator
or ``..`` can never stat a file outside the album directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from punt_vox.voxd.containment import ContainmentRoot
    from punt_vox.voxd.programs.catalog import Album
    from punt_vox.voxd.programs.part import Part

__all__ = ["AlbumContents", "PartFile"]


@final
@dataclass(frozen=True, slots=True)
class PartFile:
    """One album part on disk: its bare file name and byte count (the get manifest)."""

    name: str
    byte_count: int

    @classmethod
    def measured(cls, root: ContainmentRoot, part: Part) -> Self:
        """Return ``part`` sized from disk, validating its name within ``root``."""
        contained = root.contained_child(part.identity)
        return cls(part.identity, contained.stat().st_size)


@final
@dataclass(frozen=True, slots=True)
class AlbumContents:
    """An album's on-disk name and its ready parts -- the ``music get`` manifest."""

    name: str
    parts: tuple[PartFile, ...]

    @classmethod
    def from_album(cls, album: Album, root: ContainmentRoot) -> Self:
        """Return the get manifest: the album name and its sized ready parts."""
        parts = tuple(PartFile.measured(root, part) for part in album.ready_parts())
        return cls(album.locator, parts)
