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
:class:`~punt_vox.lux_common.HubOutageLog` takes it: these lines are
about the panel's one connection, and reading them under a second module name
would split one story across two.
"""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING, Self, final

from punt_lux import HubUnavailableError

from punt_vox.client_errors import VoxdProtocolError
from punt_vox.lux_common.hub_outage_log import HubOutageLog

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

    @asynccontextmanager
    async def control_rejection(self, what: str) -> AsyncGenerator[None]:
        """Swallow voxd refusing *what*, one control widget's change, and snap it back.

        The control-change twin of :meth:`rejection`: the same real failure, but
        the caller is a widget that already applied *what* optimistically on
        the client the instant it fired, before this refusal came back. A
        diff-based re-push (:meth:`repush`) compares against the last render
        this session successfully landed -- the still-true value the widget was
        supposed to keep showing -- so it patches only the notice and leaves the
        widget's wrong guess on screen. :meth:`correct` reinstalls in full
        instead, which is what actually reasserts the control's field.
        """
        try:
            yield
        except VoxdProtocolError as exc:
            self._note(exc, what)
            await self.correct()

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
        """Refresh the held scene in place, letting an absent luxd drop it.

        Reached behind a genuine control change and behind a click's confirming
        read, so the user is mid-interaction with a panel that is already on
        screen: it takes the patch path, which leaves the frame's stacking order
        alone. Showing here instead is what made every radio click raise the
        window.

        A fresh REST client per push is safe for that: luxd derives the connection
        id from the declared identity rather than the socket, so this client
        patches the scene an earlier one installed.
        """
        with self.outage("luxd unavailable; dropped the panel re-push"):
            await self._service.push_scene(self._rest_factory())

    async def correct(self) -> None:
        """Reinstall the held scene in full, letting an absent luxd drop it.

        Reached only behind a control-change failure: the widget already
        applied its change optimistically before voxd answered, so
        :meth:`repush`'s diff -- computed against the last render this session
        successfully landed -- would see nothing to patch and leave the
        widget's wrong guess on screen. A full reinstall reasserts every field,
        the control's included, with no frame raise (see
        :meth:`~punt_vox.panel.panel_push.PanelPush.correct`).
        """
        with self.outage("luxd unavailable; dropped the panel correction"):
            await self._service.correct_scene(self._rest_factory())

    def _note(self, exc: VoxdProtocolError, what: str) -> None:
        """Log voxd's refusal of *what* loud, and carry its reason into the scene."""
        self._logger.exception("vox-panel: voxd refused %s", what)
        self._service.note_rejection(str(exc))
