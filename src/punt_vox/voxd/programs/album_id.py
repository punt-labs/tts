"""The short unique hex identity every album carries (``AlbumId``).

An ``AlbumId`` is the album's stable handle: the directory suffix (``<slug>-<id>``)
and the catalog key. The id value-space is this type's, so the collision-avoiding
mint loop lives here and :meth:`Catalog.mint_id` delegates to it.
Construction validates the hex shape, so a wire- or filesystem-derived id can
never smuggle a path separator or non-hex junk into a directory name.
"""

from __future__ import annotations

import secrets
from collections.abc import Container
from typing import ClassVar, Final, Self, final

from punt_vox.voxd.programs.hex_token import HexToken

__all__ = ["AlbumId"]

_ID_BYTES: Final = 3  # six hex chars -- 16.7M ids, ample for a personal library


@final
class AlbumId(HexToken):
    """A short, unique, lowercase-hex album identity (``secrets.token_hex(3)``).

    Validation, the ``value`` accessor, and the value-object dunders come from
    :class:`HexToken`; this subclass adds only the collision-avoiding mint factory,
    so :meth:`Catalog.mint_id` delegates to it.
    """

    __slots__ = ()
    _LABEL: ClassVar[str] = "album id"

    @classmethod
    def mint(cls, taken: Container[AlbumId]) -> Self:
        """Return a fresh id absent from ``taken`` (owns the collision-retry loop)."""
        while True:
            candidate = cls(secrets.token_hex(_ID_BYTES))
            if candidate not in taken:
                return candidate

    @classmethod
    def try_from(cls, value: str) -> Self | None:
        """Return the id ``value`` names, or ``None`` when it is not a well-formed id.

        Unlike construction, a malformed ``value`` is not an error here: the bare
        ``music play`` positional is *id-or-name*, so a non-hex value is a curated
        name, not a broken id. Returning ``None`` lets the caller fall through to
        name resolution rather than surface a hex-validation error -- the one place
        a non-hex value is a legitimate state (a name), not a rejected input.
        """
        try:
            return cls(value)
        except ValueError:
            return None
