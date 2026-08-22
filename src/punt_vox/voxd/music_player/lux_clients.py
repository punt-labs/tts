"""``VoxLuxClients`` -- voxd's one explicit lux identity and the clients it builds.

voxd is a lux *app*: it names itself explicitly (``kind=app``, ``name=voxd``, a 30s
menu lease) instead of deriving a ``cli`` identity from a working directory. The
same identity backs both legs -- the ``LuxClient`` facade that renders scenes and
registers the menu, and the hub listener that carries the pub-sub receive stream --
so luxd resolves both to a single session, and a lease renewed by any contact keeps
the menu alive.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from punt_lux import ClientIdentity, LuxClient
from punt_lux.hub_paths import HubPaths

from punt_vox.voxd.music_player.lux_trace import LuxTrace

if TYPE_CHECKING:
    from punt_lux import CallbackHandler, EventHandler
    from punt_lux.hub_client import ConnectHandler, LuxHubClient

__all__ = ["VoxLuxClients"]

_APP_NAME = "voxd"
_LEASE_TTL_SECONDS = 30.0

_trace = LuxTrace(logging.getLogger(__name__))


@final
class VoxLuxClients:
    """Build voxd's ``LuxClient`` facade and hub listener from one app identity."""

    __slots__ = ("_identity",)
    _identity: ClientIdentity

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._identity = ClientIdentity(
            kind="app", name=_APP_NAME, lease_ttl=_LEASE_TTL_SECONDS
        )
        return self

    def client(self) -> LuxClient:
        """Build the ``LuxClient`` facade for scene push and menu registration.

        Raises ``HubUnavailableError`` when luxd is down, so callers invoke it
        lazily (the publisher) or under a retry (the menu), never at import.
        """
        _trace.info("connecting REST client at %s", self._endpoint())
        return LuxClient.for_identity(self._identity)

    def hub(
        self,
        on_event: EventHandler,
        on_callback: CallbackHandler,
        on_connect: ConnectHandler,
    ) -> LuxHubClient:
        """Build the hub listener that carries the pub-sub receive stream.

        ``on_connect`` is wired straight into the listener: it fires after every
        handshake -- first connect and every internal reconnect the ``listen`` loop
        rides out -- so the receive leg re-registers its menu and re-pushes its scene
        register-fresh, without waiting for an outer fault.

        Raises ``HubUnavailableError`` when luxd is down at construction; the
        subscription's run loop retries, so a late-starting luxd is picked up.
        """
        _trace.info("connecting hub client at %s", self._endpoint())
        return LuxClient.for_identity(self._identity).listener(
            on_callback=on_callback,
            on_event=on_event,
            on_connect=on_connect,
        )

    def _endpoint(self) -> str:
        """Return this identity's resolved luxd endpoint for a log line.

        Reads luxd's shared port file each call so the connecting line names the
        actual port the client is about to reach under its own identity; a ``None``
        port renders in place and is exactly why the imminent connect will raise
        (luxd is down).
        """
        return f"{self._identity.name}@127.0.0.1 port {HubPaths().read_port()}"
