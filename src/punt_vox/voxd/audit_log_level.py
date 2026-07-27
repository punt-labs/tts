"""Confine a requested daemon log level to the [DEBUG, INFO] band -- never NOTSET.

``vox log`` is a wire op: a token-holding client can set the running daemon's
level. That raises a risk -- a client could *lower* the daemon below the level
that records the audit trail (Synthesize/Record/Play INFO lines, ``Auth
rejected`` / ``rejected op`` WARNING, ``operation failed`` ERROR), blinding the
operator to the attacker's own rejected requests. :class:`AuditFloorLevel`
encodes the invariant as a type: the effective threshold is clamped into the
closed band ``[DEBUG, INFO]`` on *both* ends, so the floor cannot be crossed
regardless of the value a crafted frame supplies.

Both bounds matter. INFO is the audit floor -- a stricter threshold (``warning``/
``error``) would drop the INFO+ trail, so it clamps down to INFO. DEBUG is the
concrete lower bound -- a request of ``notset`` (0), or any sub-DEBUG level,
would set a *threshold of zero*; on a NOTSET **named** logger Python's logging
defers to the parent's effective level, and if that parent sits above INFO the
trail is suppressed. Flooring at DEBUG keeps the level a concrete band value that
never defers, so the guarantee holds whichever logger the applier targets.
"""

from __future__ import annotations

import logging
from typing import Self, final

__all__ = ["AuditFloorLevel"]

# The effective threshold lives in the closed band [DEBUG, INFO]:
#   * never above INFO -- the audit trail (INFO and above) is always recorded;
#   * never below DEBUG -- always a concrete level, never NOTSET(0)/defer.
_MAX_THRESHOLD = logging.INFO
_MIN_THRESHOLD = logging.DEBUG


@final
class AuditFloorLevel:
    """A daemon log level confined to the [DEBUG, INFO] band -- never NOTSET.

    Built through :meth:`from_name`, which resolves a requested level name to a
    numeric threshold and clamps it into the band. The constructor clamps any
    raw numeric too, so no path -- name or number -- can yield a level outside
    ``[DEBUG, INFO]``; :meth:`from_name` is the only way to build one from a name.
    """

    __slots__ = ("_numeric",)

    _numeric: int

    def __new__(cls, numeric: int) -> Self:
        self = super().__new__(cls)
        # Clamp both ends: cap at INFO (the audit floor) and raise to DEBUG (a
        # concrete level), so the result is never NOTSET(0) and never defers.
        self._numeric = min(max(numeric, _MIN_THRESHOLD), _MAX_THRESHOLD)
        return self

    @classmethod
    def from_name(cls, name: str) -> Self:
        """Return the band-clamped level for a requested *name*; raise on unknown.

        ``debug``/``info`` pass through; ``warning``/``error`` (which would drop
        the INFO trail) clamp down to ``info``; ``notset`` (which would set a
        defer-to-parent threshold of zero) clamps up to ``debug``. An unrecognized
        name is a rejected client request -- ``ValueError`` (PY-EH-8), not a
        silent default -- so a malformed frame is refused, not defaulted.
        """
        numeric = logging.getLevelNamesMapping().get(name.strip().upper())
        if numeric is None:
            msg = f"unknown log level: {name!r}"
            raise ValueError(msg)
        return cls(numeric)

    @property
    def numeric(self) -> int:
        """Return the clamped numeric threshold -- always within [DEBUG, INFO]."""
        return self._numeric

    @property
    def name(self) -> str:
        """Return the clamped level's lowercase name (e.g. ``info``, ``debug``)."""
        return logging.getLevelName(self._numeric).lower()
