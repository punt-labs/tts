"""``HubOutageLog`` -- turns a tight hub-retry loop into occasional, visible logging.

:class:`~punt_vox.panel.leg.VoxPanelLeg` retries a dropped hub connection every
couple of seconds. Logging every tick at a visible level would spam a healthy
log; logging every tick at DEBUG makes a real, hours-long outage produce
nothing at all under the default INFO level -- an empty log that looks like
"nothing happened" when what happened is "the panel has been unreachable the
whole time." This tracks how long the *same* outage has run and escalates:
the first tick logs at WARNING, later ticks restate at INFO no more often
than once per :data:`_RESTATE_SECONDS`, and every other tick stays at DEBUG.

``note()`` and ``clear()`` are called from both the leg's event-loop
coroutine (``_listen_once``) and its ``asyncio.to_thread`` worker (``_apply``)
on the same instance, so the read-modify-write of ``_started_at``/
``_last_logged_at`` needs the same lock discipline as
:class:`~punt_vox.panel.service.VoxPanelService`'s held state -- otherwise a
``clear()`` racing a ``note()`` can null ``_started_at`` mid-computation and
raise inside the unguarded ``serve()`` retry loop, permanently killing
reconnection for the rest of the session.
"""

from __future__ import annotations

import logging
import threading
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
    _lock: threading.Lock
    __slots__ = ("_last_logged_at", "_lock", "_logger", "_started_at")

    def __new__(cls, logger: logging.Logger) -> Self:
        self = super().__new__(cls)
        self._logger = logger
        self._started_at = None
        self._last_logged_at = 0.0
        self._lock = threading.Lock()
        return self

    def note(self, message: str) -> None:
        """Record one retry tick, logging at WARNING, INFO, or DEBUG as it ages."""
        with self._lock:
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
        with self._lock:
            self._started_at = None
