"""``LuxScenePublisher`` -- drain the scene mailbox and PUT to luxd off the writer.

:meth:`submit` runs on the control-channel single-writer and only hands the newest
scene to a latest-wins :class:`SceneMailbox` -- it never blocks. :meth:`run` is the
publisher's own task: it drains the mailbox and performs the *blocking* REST render
inside :func:`asyncio.to_thread`, so a slow luxd cannot stall the event loop (and
thus playback). A lux timeout / :class:`HubUnavailableError` is logged and dropped
and the client is dropped for a fresh reconnect; an engine-side ``OpError`` is
logged. No lux failure is ever propagated back into audio control.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self, final

from punt_lux import HubUnavailableError, OpError

from punt_vox.voxd.music_player.scene_mailbox import SceneMailbox

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_lux import LuxRestClient, RenderRequest

__all__ = ["LuxScenePublisher"]

logger = logging.getLogger(__name__)

_SCENE_ID = "vox.music"


@final
class LuxScenePublisher:
    """Own the scene mailbox and render each newest scene to luxd on its own task."""

    __slots__ = ("_client", "_connect", "_mailbox")
    _connect: Callable[[], LuxRestClient]
    _client: LuxRestClient | None  # None until first connect / after a drop
    _mailbox: SceneMailbox

    def __new__(cls, connect: Callable[[], LuxRestClient]) -> Self:
        self = super().__new__(cls)
        self._connect = connect
        self._client = None
        self._mailbox = SceneMailbox()
        return self

    def submit(self, request: RenderRequest) -> None:
        """Hand the newest scene to the mailbox -- non-blocking, writer-safe."""
        self._mailbox.submit(request)

    async def run(self) -> None:
        """Drain the mailbox forever, rendering each newest scene to luxd.

        The per-scene guard is the last line of defence: any unexpected error
        rendering one scene is logged and the loop continues, so a display fault
        never kills the publisher task (and playback is untouched regardless).
        """
        while True:
            request = await self._mailbox.get()
            try:
                await self._publish(request)
            except Exception:
                logger.exception("scene publisher: unexpected error rendering a scene")

    async def _publish(self, request: RenderRequest) -> None:
        """Connect if needed and PUT the scene off-thread, dropping any lux failure."""
        try:
            client = await self._ensure_client()
            result = await asyncio.to_thread(client.render, request)
        except HubUnavailableError:
            self._client = None  # force a reconnect on the next scene
            logger.warning("lux unavailable; dropped %s scene push", _SCENE_ID)
            return
        if isinstance(result, OpError):
            logger.warning("lux rejected %s scene: %s", _SCENE_ID, result.reason)

    async def _ensure_client(self) -> LuxRestClient:
        """Return the connected client, connecting off-thread on first use."""
        if self._client is None:
            self._client = await asyncio.to_thread(self._connect)
        return self._client
