"""The lux receive-leg transport seams: the hub listener, the menu, the REST client.

:class:`HubListener` is the one persistent WebSocket the subscription holds --
subscribe to topics, then listen (with internal reconnect) until stopped; the
concrete ``LuxHubClient`` satisfies it structurally. :class:`MenuClient` is the REST
call that registers voxd's ``Music`` menu entry. :class:`LuxClient` is their REST
union -- render a scene *and* register a callback -- so the composition root injects
one factory that both the scene publisher and the menu registrar draw from.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from punt_vox.voxd.music_player.ports import LuxRenderer

if TYPE_CHECKING:
    from punt_lux import CallbackHandler, EventHandler, OpError
    from punt_lux.hub_client import ConnectHandler
    from punt_lux.operations import Ok

__all__ = [
    "HubListener",
    "LuxClient",
    "LuxClientFactory",
    "MenuClient",
    "MenuRegistrar",
]


@runtime_checkable
class HubListener(Protocol):
    """voxd's one live hub connection: subscribe, then listen with reconnect."""

    def subscribe(self, *topics: str) -> None:
        """Record the topics to (re)subscribe on every connect; call before listen."""
        ...

    async def listen(self) -> None:
        """Hold the connection open, dispatching frames, until :meth:`stop`."""
        ...

    def stop(self) -> None:
        """Ask the listen loop to finish after its current connection closes."""
        ...


@runtime_checkable
class MenuClient(Protocol):
    """The REST surface that registers a menu callback under voxd's identity."""

    def register_callback(self, callback_id: str, label: str) -> Ok | OpError:
        """Register the ``Music`` menu entry, returning success or a typed error."""
        ...


@runtime_checkable
class LuxClient(LuxRenderer, MenuClient, Protocol):
    """The REST client that both renders scenes and registers the menu callback."""


@runtime_checkable
class MenuRegistrar(Protocol):
    """The guarded registration of one menu callback the subscription drives."""

    async def register(self, callback_id: str, label: str) -> None:
        """Register the menu callback, swallowing a down or refusing luxd."""
        ...


@runtime_checkable
class LuxClientFactory(Protocol):
    """Build voxd's two lux clients from one identity -- the composition seam.

    Injected at the composition root so a test drives both legs with fakes; the
    concrete ``VoxLuxClients`` satisfies it. Bundling the REST and hub factories
    behind one object keeps the subsystem's constructor to a single client seam.
    """

    def rest(self) -> LuxClient:
        """Build the REST client that renders scenes and registers the menu."""
        ...

    def hub(
        self,
        on_event: EventHandler,
        on_callback: CallbackHandler,
        on_connect: ConnectHandler,
    ) -> HubListener:
        """Build the hub client carrying the pub-sub receive stream.

        ``on_connect`` is fired after every handshake -- first connect and every
        internal reconnect -- so the receive leg re-registers its menu and re-pushes
        its scene register-fresh, not only on an outer fault.
        """
        ...
