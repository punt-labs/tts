"""Tests for :mod:`punt_vox.panel.panel_push`.

The property that matters is the one the user sees: a refresh must not reinstall,
because installing raises the frame. Everything else here is what has to hold for
that to stay safe -- a refusal or an outage leaves the panel's idea of what is
installed *disarmed*, so the next push installs rather than patching a tree luxd
never accepted.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast, final

import pytest
from punt_lux import HubUnavailableError, LuxClient, OpError, RenderRequest, SceneShown

from punt_vox.panel.panel_push import PanelPush

if TYPE_CHECKING:
    from punt_lux.operations import UpdateRequest


def _scene(content: str = "") -> RenderRequest:
    return RenderRequest(
        scene_id="vox.panel",
        elements=[{"kind": "text", "id": "vox.panel.status", "content": content}],
        title="Vox",
    )


@final
class _FakeSceneAccessor:
    def __init__(self, *, refuse: bool = False, down: bool = False) -> None:
        self.shown: list[RenderRequest] = []
        self.patched: list[list[dict[str, object]]] = []
        self._refuse = refuse
        self._down = down

    async def show(self, request: RenderRequest) -> SceneShown | OpError:
        if self._down:
            raise HubUnavailableError("luxd is not running")
        if self._refuse:
            return OpError(code="rejected", reason="no display")
        self.shown.append(request)
        return SceneShown(scene_id=request.scene_id)

    async def update(
        self, scene_id: str, request: UpdateRequest | OpError
    ) -> SceneShown | OpError:
        if self._down:
            raise HubUnavailableError("luxd is not running")
        if self._refuse:
            return OpError(code="rejected", reason="no display")
        assert not isinstance(request, OpError)
        self.patched.append(request.to_wire())
        return SceneShown(scene_id=scene_id)


@final
class _FakeClient:
    def __init__(self, *, refuse: bool = False, down: bool = False) -> None:
        self.scene = _FakeSceneAccessor(refuse=refuse, down=down)


def _as_client(fake: _FakeClient) -> LuxClient:
    return cast("LuxClient", fake)


class TestRefresh:
    async def test_the_first_refresh_installs(self) -> None:
        push, client = PanelPush(), _FakeClient()
        await push.refresh(_as_client(client), _scene())
        assert len(client.scene.shown) == 1

    async def test_a_changed_value_patches_without_showing(self) -> None:
        push, client = PanelPush(), _FakeClient()
        await push.refresh(_as_client(client), _scene())
        await push.refresh(_as_client(client), _scene("voxd is not reachable"))

        assert len(client.scene.shown) == 1  # no second frame raise
        assert client.scene.patched == [
            [{"id": "vox.panel.status", "set": {"content": "voxd is not reachable"}}]
        ]

    async def test_an_unchanged_render_puts_nothing_on_the_wire(self) -> None:
        push, client = PanelPush(), _FakeClient()
        await push.refresh(_as_client(client), _scene())
        await push.refresh(_as_client(client), _scene())

        assert len(client.scene.shown) == 1
        assert client.scene.patched == []


class TestInstall:
    async def test_shows_even_when_the_render_is_unchanged(self) -> None:
        push, client = PanelPush(), _FakeClient()
        await push.refresh(_as_client(client), _scene())
        await push.install(_as_client(client), _scene())

        assert len(client.scene.shown) == 2
        assert client.scene.patched == []


class TestRecovery:
    async def test_a_refusal_is_logged_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.ERROR):
            await PanelPush().refresh(_as_client(_FakeClient(refuse=True)), _scene())
        assert any("luxd rejected the scene" in r.getMessage() for r in caplog.records)

    async def test_a_refusal_disarms_so_the_next_push_installs(self) -> None:
        push = PanelPush()
        await push.refresh(_as_client(_FakeClient(refuse=True)), _scene())

        client = _FakeClient()
        await push.refresh(_as_client(client), _scene("changed"))

        assert len(client.scene.shown) == 1
        assert client.scene.patched == []

    async def test_an_absent_luxd_propagates_to_the_callers_outage_guard(self) -> None:
        with pytest.raises(HubUnavailableError):
            await PanelPush().refresh(_as_client(_FakeClient(down=True)), _scene())

    async def test_an_absent_luxd_disarms_so_the_new_connection_installs(self) -> None:
        push = PanelPush()
        with pytest.raises(HubUnavailableError):
            await push.refresh(_as_client(_FakeClient(down=True)), _scene())

        client = _FakeClient()
        await push.refresh(_as_client(client), _scene())

        assert len(client.scene.shown) == 1
        assert client.scene.patched == []
