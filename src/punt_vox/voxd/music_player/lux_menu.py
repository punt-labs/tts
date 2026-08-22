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

from punt_vox.lux_common import HubOutageLog
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

    __slots__ = ("_connect", "_outage")
    _connect: Callable[[], LuxClient]
    _outage: HubOutageLog

    def __new__(
        cls,
        connect: Callable[[], LuxClient],
        # None means the registrar throttles its own registrations; the
        # composition injects the receive-leg's outage log so a single luxd
        # outage escalates once across both sites, not twice.
        outage: HubOutageLog | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._connect = connect
        self._outage = outage if outage is not None else HubOutageLog(logger)
        return self

    async def register(self, callback_id: str, label: str) -> None:
        """Register the ``label`` menu entry, dropping any lux failure.

        This is a best-effort REST I/O boundary. A down luxd is noted through the
        shared :class:`HubOutageLog` so the first tick lands as WARNING and later
        ticks quiet to DEBUG (with a 30s INFO restatement) -- the receive leg's
        retry loop and this per-registration site share one escalation window. Any
        other transport fault -- a refused connection, a timeout, a client-side
        error -- is logged with its traceback and swallowed. Nothing is raised, so
        a failed menu registration can never escape into the receive leg's guarded
        restart and turn a missing menu into a dropped connection.
        """
        try:
            client = self._connect()
            result = await client.callback.register(callback_id, label)
        except HubUnavailableError:
            self._outage.note(
                f"[lux] luxd unavailable; {label!r} menu entry not registered"
            )
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
