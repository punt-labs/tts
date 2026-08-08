"""``PanelMenuEntry`` -- the ``Vox`` entry in the Lux menu and the call that puts it up.

Registration is the leg's one blocking RPC made *inside* a handshake rather
than off it, and that placement is what makes its failure boundary matter.
``on_connect`` is awaited by the hub client inside a blanket handler that
deliberately leaves the socket up when it fires -- an app-logic bug must not
become a reconnect storm against luxd -- so a failure escaping the callback is
logged under that library's name, the leg's retry loop never re-fires, and the
menu carries no entry for the rest of the connection's life with nothing in
the panel's own log to say why. The boundary therefore lives next to the call
it guards, the way :class:`~punt_vox.panel.panel_runner.PanelRunner` carries
one for each piece of work the leg starts.

The logger is the leg's, injected as
:class:`~punt_vox.panel.panel_guard.PanelGuard` takes it: these lines are
about the panel's one connection, and reading them under a second module name
would split one story across two.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Self, final

from punt_lux import OpError

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable

    from punt_lux.operations import Ok

    from punt_vox.panel.panel_guard import PanelGuard
    from punt_vox.panel.ports import PanelRestClient
    from punt_vox.panel.service import VoxPanelService

__all__ = ["PanelMenuEntry"]

_UNAVAILABLE_MESSAGE = "luxd went away before the panel's menu entry landed"


@final
class PanelMenuEntry:
    """The panel's menu entry: the call that puts it up, and how it can fail."""

    _service: VoxPanelService
    _rest_factory: Callable[[], PanelRestClient]
    _guard: PanelGuard
    _logger: logging.Logger
    __slots__ = ("_guard", "_logger", "_rest_factory", "_service")

    def __new__(
        cls,
        service: VoxPanelService,
        rest_factory: Callable[[], PanelRestClient],
        guard: PanelGuard,
        logger: logging.Logger,
    ) -> Self:
        self = super().__new__(cls)
        self._service = service
        self._rest_factory = rest_factory
        self._guard = guard
        self._logger = logger
        return self

    async def registered(self) -> bool:
        """Put the entry up off the loop; answer whether it is there to be clicked.

        One answer covers all three ways it can be absent -- luxd away, luxd
        refusing, a bug -- because whatever waits behind the entry waits on
        exactly one thing: somebody being able to click it.
        """
        try:
            with self._guard.outage(_UNAVAILABLE_MESSAGE):
                return self._landed(await asyncio.to_thread(self._register_now))
        except Exception:
            self._logger.exception("the panel's menu entry could not be registered")
        return False

    def _landed(self, result: Ok | OpError) -> bool:
        """Answer whether *result* put the entry up, logging a refusal loud."""
        if isinstance(result, OpError):
            self._logger.error("the panel's menu entry was refused: %s", result.reason)
            return False
        return True

    def _register_now(self) -> Ok | OpError:
        return self._rest_factory().register_callback(
            self._service.callback_id, self._service.label
        )
