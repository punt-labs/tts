"""``PanelGuard`` -- the panel's answer to the failures two other processes hand it.

luxd being away and voxd answering with a refusal are different failures and
get different answers. An outage is a transient the next retry tick sweeps up,
so it is swallowed and throttled into one ongoing report. A refusal is a real
failure -- a voice voxd no longer knows, a reply the client cannot read -- so
it is logged loud and carried into the scene, where whoever clicked can read
the reason voxd gave. Both answers live here, which leaves
:class:`~punt_vox.panel.leg.VoxPanelLeg` about the connection it serves rather
than about the ways it can be let down.

The logger is the leg's, injected the same way
:class:`~punt_vox.panel.hub_outage_log.HubOutageLog` takes it: these lines are
about the panel's one connection, and reading them under a second module name
would split one story across two.
"""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING, Self, final

from punt_lux import HubUnavailableError

from punt_vox.client_errors import VoxdProtocolError
from punt_vox.panel.hub_outage_log import HubOutageLog

if TYPE_CHECKING:
    import logging
    from collections.abc import AsyncGenerator, Callable, Generator

    from punt_lux import LuxClient

    from punt_vox.panel.service import VoxPanelService

__all__ = ["PanelGuard"]


@final
class PanelGuard:
    """Swallow luxd being away and voxd refusing, each answered in its own way."""

    _service: VoxPanelService
    _rest_factory: Callable[[], LuxClient]
    _logger: logging.Logger
    _outage: HubOutageLog
    __slots__ = ("_logger", "_outage", "_rest_factory", "_service")

    def __new__(
        cls,
        service: VoxPanelService,
        rest_factory: Callable[[], LuxClient],
        logger: logging.Logger,
    ) -> Self:
        self = super().__new__(cls)
        self._service = service
        self._rest_factory = rest_factory
        self._logger = logger
        self._outage = HubOutageLog(logger)
        return self

    def connected(self) -> None:
        """Note that luxd answered, so the next outage opens its own report."""
        self._outage.clear()

    @contextmanager
    def outage(self, message: str) -> Generator[None]:
        """Swallow luxd being away, noting it as one tick of an ongoing outage."""
        try:
            yield
        except HubUnavailableError:
            self._outage.note(message)

    @asynccontextmanager
    async def rejection(self, what: str) -> AsyncGenerator[None]:
        """Swallow voxd refusing *what*, correcting the scene to say so instead.

        The opposite of :meth:`outage`, which swallows a transient the next tick
        retries away: voxd answering with a refusal is a real failure, so it is
        logged loud AND pushed back into the scene, never reduced to a log line
        by the blanket handler around the caller.
        """
        try:
            yield
        except VoxdProtocolError as exc:
            self._note(exc, what)
            await self.repush()

    @contextmanager
    def offscreen_rejection(self, what: str) -> Generator[None]:
        """Swallow voxd refusing *what* with no scene up yet: note it, push nothing.

        The same real failure :meth:`rejection` catches, one connection-time
        difference: nothing is on screen to correct. A push here would open
        the panel nobody clicked for and fill it with the defaults held before
        the first read, as if those were the session's settings -- so the
        notice waits for the first click's own push, which is when someone is
        looking at it anyway.
        """
        try:
            yield
        except VoxdProtocolError as exc:
            self._note(exc, what)

    async def repush(self) -> None:
        """Send the held scene back out, letting an absent luxd drop it."""
        with self.outage("luxd unavailable; dropped the panel re-push"):
            await self._service.push_scene(self._rest_factory())

    def _note(self, exc: VoxdProtocolError, what: str) -> None:
        """Log voxd's refusal of *what* loud, and carry its reason into the scene."""
        self._logger.exception("vox-panel: voxd refused %s", what)
        self._service.note_rejection(str(exc))
