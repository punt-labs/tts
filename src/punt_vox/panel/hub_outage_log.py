"""``HubOutageLog`` -- turns a tight hub-retry loop into occasional, visible logging.

:class:`~punt_vox.panel.leg.VoxPanelLeg` retries a dropped hub connection every
couple of seconds. Logging every tick at a visible level would spam a healthy
log; logging every tick at DEBUG makes a real, hours-long outage produce
nothing at all under the default INFO level -- an empty log that looks like
"nothing happened" when what happened is "the panel has been unreachable the
whole time." This tracks how long the *same* outage has run and escalates:
the first tick logs at WARNING, later ticks restate at INFO no more often
than once per :data:`_RESTATE_SECONDS`, and every other tick stays at DEBUG.
"""

from __future__ import annotations

import logging
import time
from typing import Self, final

__all__ = ["HubOutageLog"]

_RESTATE_SECONDS = 30.0


@final
class HubOutageLog:
    """Tracks one ongoing hub-unavailable outage and decides how loud to log it."""

    _logger: logging.Logger
    _started_at: float | None  # None means no outage is currently open
    _last_logged_at: float
    __slots__ = ("_last_logged_at", "_logger", "_started_at")

    def __new__(cls, logger: logging.Logger) -> Self:
        self = super().__new__(cls)
        self._logger = logger
        self._started_at = None
        self._last_logged_at = 0.0
        return self

    def note(self, message: str) -> None:
        """Record one retry tick, logging at WARNING, INFO, or DEBUG as it ages."""
        now = time.monotonic()
        if self._started_at is None:
            self._started_at = now
            self._last_logged_at = now
            self._logger.warning("%s", message)
            return
        if now - self._last_logged_at >= _RESTATE_SECONDS:
            self._last_logged_at = now
            elapsed = now - self._started_at
            self._logger.info("%s (ongoing for %.0fs)", message, elapsed)
            return
        self._logger.debug("%s", message)

    def clear(self) -> None:
        """Record that the hub is reachable again, closing the outage window."""
        self._started_at = None
