"""``LuxTrace`` -- the one ``[lux]``-prefixed logger for the music-player lux legs.

Every lifecycle line the lux legs emit carries a single greppable ``[lux]`` prefix,
so one ``grep '\\[lux\\]' vox.log`` replays the whole connect / subscribe / push /
inbound-event / disconnect / reconnect story of voxd's lux legs in order. This class
owns that prefix and the level convention -- INFO for a normal transition, WARNING
for a recoverable fault, ERROR for a refusal, ``exception`` for the unexpected -- so
no call site repeats the literal and the levels can never drift across the six leg
modules that share it.

The class composes a module logger rather than subclassing one: each leg passes its
own ``getLogger(__name__)`` so the ``client.<role>.`` / daemon name prefix and the
per-module grep still work, while ``%s``-style args stay lazy (the prefix is prepended
to the format template, never to the interpolated result).

Traceback lines stay on the raw ``logger.exception`` at each call site (ruff BLE001
recognises the logger there, and only inside an ``except`` block is the active
exception available); those messages carry the same ``[lux]`` literal, so one grep
still spans the normal transitions here and the faults there.
"""

from __future__ import annotations

import logging
from typing import Self, final

__all__ = ["LuxTrace"]

_PREFIX = "[lux] "


@final
class LuxTrace:
    """Emit one ``[lux]``-prefixed lifecycle line per lux-leg transition."""

    __slots__ = ("_logger",)
    _logger: logging.Logger

    def __new__(cls, logger: logging.Logger) -> Self:
        self = super().__new__(cls)
        self._logger = logger
        return self

    def info(self, template: str, *args: object) -> None:
        """Log a normal lifecycle transition at INFO with the ``[lux]`` prefix."""
        self._logger.info(_PREFIX + template, *args)

    def warning(self, template: str, *args: object) -> None:
        """Log a recoverable fault (down/retrying luxd) at WARNING, ``[lux]``-tagged."""
        self._logger.warning(_PREFIX + template, *args)

    def error(self, template: str, *args: object) -> None:
        """Log a refused operation at ERROR with the ``[lux]`` prefix."""
        self._logger.error(_PREFIX + template, *args)
