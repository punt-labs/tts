"""The structural seams the panel package depends on.

``PanelRestClient`` is the REST surface :class:`~punt_vox.panel.leg.VoxPanelLeg`
needs -- render a scene, register the menu callback, and build the persistent
listener. ``SettingsSource`` and ``VoiceRoster`` are the two reads
:class:`~punt_vox.panel.state.PanelState` needs -- the config store and the
daemon's voice list. Each lets a test drive its dependent with an in-memory
fake; the concrete ``punt_lux.rest_client.LuxRestClient``, ``ConfigStore``, and
``VoxClientSync`` satisfy them structurally.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from punt_lux import OpError, RenderRequest, SceneShown
    from punt_lux.hub_client import CallbackHandler, ConnectHandler, EventHandler
    from punt_lux.operations import Ok

    from punt_vox.config import VoxConfig

__all__ = ["HubListener", "PanelRestClient", "SettingsSource", "VoiceRoster"]


@runtime_checkable
class HubListener(Protocol):
    """The one live hub connection: subscribe topics, then listen with reconnect."""

    def subscribe(self, *topics: str) -> None:
        """Record the topics to (re)subscribe on every connect; call before listen."""
        ...

    async def listen(self) -> None:
        """Hold the connection open, dispatching frames, until it is stopped."""
        ...


@runtime_checkable
class PanelRestClient(Protocol):
    """The REST surface the panel's leg needs: render, register, and listen."""

    def render(self, request: RenderRequest) -> SceneShown | OpError:
        """Install a whole scene, returning the result or a typed error."""
        ...

    def register_callback(self, callback_id: str, label: str) -> Ok | OpError:
        """Register a menu callback, returning success or a typed error."""
        ...

    def listener(
        self,
        *,
        on_callback: CallbackHandler,
        on_event: EventHandler,
        on_connect: ConnectHandler | None = None,
    ) -> HubListener:
        """Build the persistent listener sharing this client's identity."""
        ...


@runtime_checkable
class SettingsSource(Protocol):
    """The config-store read :class:`~punt_vox.panel.state.PanelState` needs."""

    def read(self) -> VoxConfig:
        """Return every config field, merging the durable and ephemeral files."""
        ...


@runtime_checkable
class VoiceRoster(Protocol):
    """The daemon read :class:`~punt_vox.panel.state.PanelState` needs."""

    def voices(self) -> list[str]:
        """Return the active provider's available voice names."""
        ...
