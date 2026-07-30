"""Tests for VoxLuxClients: the explicit app identity and its two client factories.

The concrete clients reach a real luxd, so the tests assert the factory shape and
that both legs fail loud when luxd is down (the state the subscription's run loop
retries and the publisher/menu swallow).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from punt_lux import HubUnavailableError

from punt_vox.voxd.music_player.hub_ports import LuxClientFactory
from punt_vox.voxd.music_player.lux_clients import VoxLuxClients

if TYPE_CHECKING:
    from collections.abc import Mapping


def _no_port(_self: object) -> None:
    """Stand in for ``HubPaths.read_port`` when luxd is down (no port file)."""


def _port_8080(_self: object) -> int:
    """Stand in for ``HubPaths.read_port`` resolving luxd's REST port."""
    return 8080


def _port_9090(_self: object) -> int:
    """Stand in for ``HubPaths.read_port`` resolving luxd's hub port."""
    return 9090


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

    async def on_connect() -> None: ...

    with pytest.raises(HubUnavailableError):
        VoxLuxClients().hub(on_event, on_callback, on_connect)


def test_rest_logs_the_connect_with_resolved_port(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The connecting audit line names the resolved port so a grep of vox.log shows
    # exactly where each leg reached luxd.
    monkeypatch.setattr("punt_lux.hub_paths.HubPaths.read_port", _port_8080)
    with caplog.at_level(logging.INFO):
        VoxLuxClients().rest()
    connecting = [
        r
        for r in caplog.records
        if "[lux]" in r.getMessage() and "connecting REST" in r.getMessage()
    ]
    assert connecting
    assert "port 8080" in connecting[-1].getMessage()


def test_hub_logs_the_connect_with_resolved_port(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("punt_lux.hub_paths.HubPaths.read_port", _port_9090)

    async def on_event(topic: str, payload: Mapping[str, object]) -> None: ...

    async def on_callback(callback_id: str) -> None: ...

    async def on_connect() -> None: ...

    with caplog.at_level(logging.INFO):
        VoxLuxClients().hub(on_event, on_callback, on_connect)
    connecting = [
        r
        for r in caplog.records
        if "[lux]" in r.getMessage() and "connecting hub" in r.getMessage()
    ]
    assert connecting
    assert "port 9090" in connecting[-1].getMessage()
