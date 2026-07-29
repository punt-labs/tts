"""``LuxMenuRegistrar`` -- register voxd's ``Music`` menu entry, best-effort.

The mirror of :class:`LuxScenePublisher` for the menu leg: a thin, guarded transport
over the public REST client. :meth:`register` builds the client and calls
``register_callback`` off-thread (the call blocks), and swallows a down luxd or a
refused registration into a log line -- a missing menu entry must never crash the
receive leg or the daemon. The lux lease keeps the entry alive once registered; a
fresh voxd re-registers on start (Z model *register-fresh*).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self, final

from punt_lux import HubUnavailableError, OpError

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.voxd.music_player.hub_ports import MenuClient

__all__ = ["LuxMenuRegistrar"]

logger = logging.getLogger(__name__)


@final
class LuxMenuRegistrar:
    """Register one menu callback over the public REST client, failure-tolerant."""

    __slots__ = ("_connect",)
    _connect: Callable[[], MenuClient]

    def __new__(cls, connect: Callable[[], MenuClient]) -> Self:
        self = super().__new__(cls)
        self._connect = connect
        return self

    async def register(self, callback_id: str, label: str) -> None:
        """Register the ``label`` menu entry off-thread, dropping any lux failure."""
        try:
            client = await asyncio.to_thread(self._connect)
            result = await asyncio.to_thread(
                client.register_callback, callback_id, label
            )
        except HubUnavailableError:
            logger.warning("lux unavailable; %r menu entry not registered", label)
            return
        if isinstance(result, OpError):
            logger.error(
                "lux rejected the %r menu registration: %s", label, result.reason
            )
