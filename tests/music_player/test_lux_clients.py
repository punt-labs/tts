"""Tests for VoxLuxClients: the explicit app identity and its two client factories.

The concrete clients reach a real luxd, so the tests assert the factory shape and
that both legs fail loud when luxd is down (the state the subscription's run loop
retries and the publisher/menu swallow).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from punt_lux import HubUnavailableError

from punt_vox.voxd.music_player.hub_ports import LuxClientFactory
from punt_vox.voxd.music_player.lux_clients import VoxLuxClients

if TYPE_CHECKING:
    from collections.abc import Mapping


def _no_port(_self: object) -> None:
    """Stand in for ``HubPaths.read_port`` when luxd is down (no port file)."""


def test_vox_lux_clients_is_a_client_factory() -> None:
    assert isinstance(VoxLuxClients(), LuxClientFactory)


def test_rest_raises_when_luxd_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("punt_lux.hub_paths.HubPaths.read_port", _no_port)
    with pytest.raises(HubUnavailableError):
        VoxLuxClients().rest()


def test_hub_raises_when_luxd_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("punt_lux.hub_paths.HubPaths.read_port", _no_port)

    async def on_event(topic: str, payload: Mapping[str, object]) -> None: ...

    async def on_callback(callback_id: str) -> None: ...

    with pytest.raises(HubUnavailableError):
        VoxLuxClients().hub(on_event, on_callback)
