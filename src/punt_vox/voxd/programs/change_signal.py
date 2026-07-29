"""``ChangeSignal`` -- the voxd-internal publish-subscribe seam for state changes.

The single-writer control channel and the album catalog both fire this signal
after they mutate observable state; every registered :class:`ChangeListener` is
notified so a projection (the lux music scene) re-derives itself. A listener that
raises is logged and skipped, never allowed to break the writer that fired the
notification -- the fan-out is fail-soft by contract (PY-DP-8).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from punt_vox.voxd.programs.change_listener import ChangeListener

__all__ = ["ChangeSignal"]

logger = logging.getLogger(__name__)


@final
class ChangeSignal:
    """Fan a "state changed" notification out to every registered listener."""

    __slots__ = ("_listeners",)
    _listeners: list[ChangeListener]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._listeners = []
        return self

    def subscribe(self, listener: ChangeListener) -> None:
        """Register ``listener`` to receive every future change notification."""
        self._listeners.append(listener)

    def emit(self) -> None:
        """Notify every listener; a raising listener is logged, never propagated.

        This runs inside the control-channel single-writer, so one misbehaving
        subscriber must not take the writer down or stall playback.
        """
        for listener in self._listeners:
            try:
                listener.notify_changed()
            except Exception:
                logger.exception("change listener raised; skipping it")
