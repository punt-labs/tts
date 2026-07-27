"""Clamp a requested daemon log level so the INFO audit trail is never silenced.

``vox log`` is a wire op: a token-holding client can set the running daemon's
level. That raises a risk -- a client could *lower* the daemon below the level
that records the audit trail (Synthesize/Record/Play INFO lines, ``Auth
rejected`` / ``rejected op`` WARNING, ``operation failed`` ERROR), blinding the
operator to the attacker's own rejected requests. :class:`AuditFloorLevel`
encodes the invariant as a type: a requested level is honored at or below INFO
(``debug`` stays allowed) but clamped down to INFO if stricter, so the floor
cannot be crossed regardless of the value a crafted frame supplies.
"""

from __future__ import annotations

import logging
from typing import Self, final

__all__ = ["AuditFloorLevel"]

# INFO is the audit floor: records at INFO and above are the trail an operator
# relies on, so the daemon's threshold must never rise above it.
_FLOOR = logging.INFO


@final
class AuditFloorLevel:
    """A daemon log level clamped to the INFO audit floor -- never stricter.

    Built through :meth:`from_name`, which resolves a requested level name to a
    numeric threshold and caps it at INFO. The constructor takes the already-
    clamped numeric; :meth:`from_name` is the only way to build one from a name.
    """

    __slots__ = ("_numeric",)

    _numeric: int

    def __new__(cls, numeric: int) -> Self:
        self = super().__new__(cls)
        self._numeric = min(numeric, _FLOOR)
        return self

    @classmethod
    def from_name(cls, name: str) -> Self:
        """Return the clamped level for a requested level *name*; raise on unknown.

        ``debug``/``info`` pass through; ``warning``/``error`` (which would drop
        the INFO trail) clamp to ``info``. An unrecognized name is a rejected
        client request -- ``ValueError`` (PY-EH-8), not a silent default -- so a
        malformed frame is refused rather than defaulted to some arbitrary level.
        """
        numeric = logging.getLevelNamesMapping().get(name.strip().upper())
        if numeric is None:
            msg = f"unknown log level: {name!r}"
            raise ValueError(msg)
        return cls(numeric)

    @property
    def numeric(self) -> int:
        """Return the clamped numeric threshold -- never above the INFO floor."""
        return self._numeric

    @property
    def name(self) -> str:
        """Return the clamped level's lowercase name (e.g. ``info``, ``debug``)."""
        return logging.getLevelName(self._numeric).lower()
