"""``VoxLuxClients`` -- voxd's one explicit lux identity and the clients it builds.

voxd is a lux *app*: it names itself explicitly (``kind=app``, ``name=voxd``, a 30s
menu lease) instead of deriving a ``cli`` identity from a working directory. The same
identity backs both legs -- the REST client that renders scenes and registers the
menu, and the hub client that carries the pub-sub receive stream -- so luxd resolves
both to a single session, and a lease renewed by any contact keeps the menu alive.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from punt_lux import ClientIdentity, LuxHubClient, LuxRestClient

if TYPE_CHECKING:
    from punt_lux import CallbackHandler, EventHandler

    from punt_vox.voxd.music_player.hub_ports import HubListener, LuxClient

__all__ = ["VoxLuxClients"]

_APP_NAME = "voxd"
_LEASE_TTL_SECONDS = 30.0


@final
class VoxLuxClients:
    """Build voxd's REST and hub lux clients from one explicit app identity."""

    __slots__ = ("_identity",)
    _identity: ClientIdentity

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._identity = ClientIdentity(
            kind="app", name=_APP_NAME, lease_ttl=_LEASE_TTL_SECONDS
        )
        return self

    def rest(self) -> LuxClient:
        """Build the REST client that renders scenes and registers the menu.

        Raises ``HubUnavailableError`` when luxd is down, so callers invoke it
        lazily (the publisher) or under a retry (the menu), never at import.
        """
        return LuxRestClient.for_identity(self._identity)

    def hub(self, on_event: EventHandler, on_callback: CallbackHandler) -> HubListener:
        """Build the hub client that carries the pub-sub receive stream.

        Raises ``HubUnavailableError`` when luxd is down at construction; the
        subscription's run loop retries, so a late-starting luxd is picked up.
        """
        return LuxHubClient.connect(
            self._identity, on_callback=on_callback, on_event=on_event
        )
