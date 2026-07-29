"""``SceneMailbox`` -- a latest-wins one-slot handoff from the writer to luxd.

The control-channel single-writer must never block on a slow display, so it only
:meth:`submit`s the freshly projected scene here (synchronous, non-blocking) and
returns. The publisher's own task :meth:`get`s the newest scene, coalescing every
intermediate submitted since the last drain to the latest -- so a stalled push can
never back pressure the writer, and a burst of state changes collapses to one PUT.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from punt_lux import RenderRequest

__all__ = ["SceneMailbox"]

logger = logging.getLogger(__name__)


@final
class SceneMailbox:
    """Hold only the newest submitted scene; the drainer awaits and takes it."""

    __slots__ = ("_latest", "_ready")
    _latest: RenderRequest | None  # the newest scene; None only before the first
    _ready: asyncio.Event

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._latest = None
        self._ready = asyncio.Event()
        return self

    def submit(self, request: RenderRequest) -> None:
        """Store ``request`` as the newest scene and wake the drainer (non-blocking)."""
        self._latest = request
        self._ready.set()

    async def get(self) -> RenderRequest:
        """Await and return the newest submitted scene, coalescing intermediates.

        A wake with no scene is unreachable in the single-threaded event loop
        (``submit`` sets ``_latest`` before the event). Should that invariant
        ever break, self-heal: log and re-await the next submit rather than
        raise, so the drainer never dies silently on a spurious wakeup.
        """
        while True:
            await self._ready.wait()
            self._ready.clear()
            request = self._latest
            if request is not None:
                return request
            logger.warning("scene mailbox woke with no scene; awaiting next submit")
