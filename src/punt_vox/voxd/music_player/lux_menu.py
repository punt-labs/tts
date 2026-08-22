"""``LuxMenuRegistrar`` -- register voxd's ``Music`` menu entry, best-effort.

The mirror of :class:`LuxScenePublisher` for the menu leg: a thin, guarded transport
over the ``LuxClient`` facade. :meth:`register` builds the client and awaits
``client.callback.register`` -- natively async on the facade -- and swallows a down
luxd or a refused registration into a log line: a missing menu entry must never
crash the receive leg or the daemon. The lux lease keeps the entry alive once
registered; a fresh voxd re-registers on start (Z model *register-fresh*).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from punt_lux import HubUnavailableError, OpError

from punt_vox.voxd.music_player.lux_trace import LuxTrace

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_lux import LuxClient

__all__ = ["LuxMenuRegistrar"]

logger = logging.getLogger(__name__)
_trace = LuxTrace(logger)


@final
class LuxMenuRegistrar:
    """Register one menu callback over the ``LuxClient`` facade, failure-tolerant."""

    __slots__ = ("_connect",)
    _connect: Callable[[], LuxClient]

    def __new__(cls, connect: Callable[[], LuxClient]) -> Self:
        self = super().__new__(cls)
        self._connect = connect
        return self

    async def register(self, callback_id: str, label: str) -> None:
        """Register the ``label`` menu entry, dropping any lux failure.

        This is a best-effort REST I/O boundary. A down luxd logs a warning; any
        other transport fault -- a refused connection, a timeout, a client-side
        error -- is logged with its traceback and swallowed. Nothing is raised, so a
        failed menu registration can never escape into the receive leg's guarded
        restart and turn a missing menu into a dropped connection.
        """
        try:
            client = self._connect()
            result = await client.callback.register(callback_id, label)
        except HubUnavailableError:
            _trace.warning("luxd unavailable; %r menu entry not registered", label)
            return
        except Exception:
            logger.exception("[lux] %r menu registration failed", label)
            return
        if isinstance(result, OpError):
            _trace.error(
                "luxd rejected the %r menu registration: %s", label, result.reason
            )
            return
        _trace.info("registered the %r menu entry (callback id %r)", label, callback_id)
