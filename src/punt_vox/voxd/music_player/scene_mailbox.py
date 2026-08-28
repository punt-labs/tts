"""``SceneMailbox`` -- a latest-wins one-slot handoff from the writer to luxd.

The control-channel single-writer must never block on a slow display, so it only
:meth:`submit`s the freshly projected scene here (synchronous, non-blocking) and
returns. The publisher's own task :meth:`get`s the newest scene, coalescing every
intermediate submitted since the last drain to the latest -- so a stalled push can
never back pressure the writer, and a burst of state changes collapses to one PUT.

The *install intent* is sticky across that coalescing, and deliberately so. Most
submits are refreshes, which patch the installed scene and leave the frame where
the user put it; a menu click or a hub handshake instead asks for an install,
which raises the frame. If a click and three refreshes coalesce into one drain,
dropping the click's intent would swallow the very gesture that meant "bring this
window to me". So the intent is OR-ed in as scenes arrive and cleared only when
the drainer takes the delivery.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from punt_lux import RenderRequest

__all__ = ["SceneDelivery", "SceneMailbox"]

logger = logging.getLogger(__name__)


@final
@dataclass(frozen=True, slots=True)
class SceneDelivery:
    """One drained scene and whether it must be installed rather than patched."""

    request: RenderRequest
    install: bool


@final
class SceneMailbox:
    """Hold only the newest submitted scene; the drainer awaits and takes it."""

    __slots__ = ("_install", "_latest", "_ready")
    # PY-TS-14: ``None`` is the pre-first-submit state, not a missing value.
    _latest: RenderRequest | None
    _install: bool
    _ready: asyncio.Event

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._latest = None
        self._install = False
        self._ready = asyncio.Event()
        return self

    def submit(self, request: RenderRequest) -> None:
        """Store ``request`` as the newest scene and wake the drainer (non-blocking)."""
        self._store(request, install=False)

    def reinstall(self, request: RenderRequest) -> None:
        """Store ``request``, demanding a full install -- a gesture or a handshake."""
        self._store(request, install=True)

    async def get(self) -> SceneDelivery:
        """Await and return the newest submitted scene, coalescing intermediates.

        A wake with no scene is unreachable in the single-threaded event loop
        (both writers set ``_latest`` before the event). Should that invariant
        ever break, self-heal: log and re-await the next submit rather than
        raise, so the drainer never dies silently on a spurious wakeup.

        The intent is cleared when the delivery is *taken*, not when its push
        succeeds, so a click whose install then fails would leave nothing here to
        retry. That is safe only because ``LuxScenePublisher`` disarms its
        ``LiveScene`` on both the outage and the refusal paths, which makes the
        next push an install regardless of what this mailbox remembers. Drop that
        disarm and the intent would have to survive a failed push instead.
        """
        while True:
            await self._ready.wait()
            self._ready.clear()
            request = self._latest
            if request is not None:
                delivery = SceneDelivery(request, self._install)
                self._install = False
                return delivery
            logger.warning("scene mailbox woke with no scene; awaiting next submit")

    def _store(self, request: RenderRequest, *, install: bool) -> None:
        """Replace the held scene, keeping any install intent already asked for."""
        self._latest = request
        self._install = self._install or install
        self._ready.set()
