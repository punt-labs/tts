"""Tests for LuxMenuRegistrar: the guarded, failure-tolerant menu registration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast, final

from punt_lux import HubUnavailableError, LuxClient, OpError
from punt_lux.operations import Ok

from punt_vox.voxd.music_player.lux_menu import LuxMenuRegistrar

if TYPE_CHECKING:
    import pytest


@final
class _OkCallbackAccessor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def register(self, callback_id: str, label: str) -> Ok | OpError:
        self.calls.append((callback_id, label))
        return Ok()


@final
class _OkClient:
    """A LuxClient stand-in whose callback accessor records and reports success."""

    def __init__(self) -> None:
        self.callback = _OkCallbackAccessor()


@final
class _RefusingCallbackAccessor:
    async def register(self, callback_id: str, label: str) -> Ok | OpError:
        return OpError(code="invalid_request", reason="bad menu id")


@final
class _RefusingClient:
    def __init__(self) -> None:
        self.callback = _RefusingCallbackAccessor()


def _as_client(fake: object) -> LuxClient:
    """Cast a duck-typed menu stand-in to the LuxClient type the seam wants."""
    return cast("LuxClient", fake)


async def test_register_calls_the_client_on_success() -> None:
    client = _OkClient()
    await LuxMenuRegistrar(lambda: _as_client(client)).register("music", "Music")
    assert client.callback.calls == [("music", "Music")]


async def test_register_logs_the_success_with_the_callback_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        await LuxMenuRegistrar(lambda: _as_client(_OkClient())).register(
            "music", "Music"
        )
    registered = [
        r
        for r in caplog.records
        if "[lux]" in r.getMessage() and "registered" in r.getMessage()
    ]
    assert registered
    assert "'music'" in registered[-1].getMessage()  # the callback id is logged


async def test_register_swallows_a_down_luxd(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def connect() -> LuxClient:
        raise HubUnavailableError("luxd down")

    with caplog.at_level(logging.WARNING):
        await LuxMenuRegistrar(connect).register("music", "Music")  # must not raise

    assert any("not registered" in r.getMessage() for r in caplog.records)


async def test_register_logs_a_refused_registration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR):
        await LuxMenuRegistrar(lambda: _as_client(_RefusingClient())).register(
            "music", "Music"
        )

    assert any("rejected" in r.getMessage() for r in caplog.records)
