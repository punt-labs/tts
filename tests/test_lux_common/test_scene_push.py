"""Tests for :mod:`punt_vox.lux_common.scene_push`.

Each push completes itself: an install shows, a patch updates and re-installs when
luxd refuses the batch, and the null push touches the client not at all.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast, final

from punt_lux import HubUnavailableError, LuxClient, OpError, RenderRequest, SceneShown

from punt_vox.lux_common.scene_patch import ElementPatch, ScenePatchSet
from punt_vox.lux_common.scene_push import InstallScene, NoPush, PatchScene

if TYPE_CHECKING:
    import pytest
    from punt_lux.operations import UpdateRequest


def _scene() -> RenderRequest:
    return RenderRequest(
        scene_id="vox.music",
        elements=[{"kind": "text", "id": "music.status", "content": ""}],
        title="Music",
    )


def _patches() -> ScenePatchSet:
    return ScenePatchSet((ElementPatch("music.status", {"content": "boom"}),))


@final
class _FakeSceneAccessor:
    """Records every show and update, answering both with success by default."""

    def __init__(self, *, refuse_update: bool = False) -> None:
        self.shown: list[RenderRequest] = []
        self.updated: list[tuple[str, list[dict[str, object]]]] = []
        self._refuse_update = refuse_update

    async def show(self, request: RenderRequest) -> SceneShown | OpError:
        self.shown.append(request)
        return SceneShown(scene_id=request.scene_id)

    async def update(
        self, scene_id: str, request: UpdateRequest | OpError
    ) -> SceneShown | OpError:
        assert not isinstance(request, OpError)
        self.updated.append((scene_id, request.to_wire()))
        if self._refuse_update:
            return OpError(code="not_found", reason="no such scene")
        return SceneShown(scene_id=scene_id)


@final
class _FakeClient:
    def __init__(self, *, refuse_update: bool = False) -> None:
        self.scene = _FakeSceneAccessor(refuse_update=refuse_update)


@final
class _DownSceneAccessor:
    async def show(self, request: RenderRequest) -> SceneShown | OpError:
        raise HubUnavailableError("luxd is not running")


@final
class _DownClient:
    def __init__(self) -> None:
        self.scene = _DownSceneAccessor()


def _as_client(fake: object) -> LuxClient:
    return cast("LuxClient", fake)


class TestInstallScene:
    async def test_shows_the_whole_request(self) -> None:
        client = _FakeClient()
        assert await InstallScene(_scene()).apply(_as_client(client)) is None
        assert [r.scene_id for r in client.scene.shown] == ["vox.music"]

    async def test_a_refusal_is_returned_not_raised(self) -> None:
        @final
        class _Refusing:
            async def show(self, request: RenderRequest) -> SceneShown | OpError:
                return OpError(code="rejected", reason="bad element")

        @final
        class _RefusingClient:
            def __init__(self) -> None:
                self.scene = _Refusing()

        refusal = await InstallScene(_scene()).apply(_as_client(_RefusingClient()))
        assert refusal is not None
        assert refusal.reason == "bad element"

    async def test_an_absent_luxd_propagates(self) -> None:
        try:
            await InstallScene(_scene()).apply(_as_client(_DownClient()))
        except HubUnavailableError:
            return
        msg = "HubUnavailableError should reach the caller that owns the client"
        raise AssertionError(msg)

    def test_summary_names_the_scene_and_its_size(self) -> None:
        assert (
            InstallScene(_scene()).summary == "installed vox.music scene (1 elements)"
        )


class TestPatchScene:
    async def test_updates_the_installed_scene_without_showing(self) -> None:
        client = _FakeClient()
        assert await PatchScene(_scene(), _patches()).apply(_as_client(client)) is None
        assert client.scene.updated == [
            ("vox.music", [{"id": "music.status", "set": {"content": "boom"}}])
        ]
        assert client.scene.shown == []

    async def test_a_refused_patch_falls_back_to_showing_the_same_request(self) -> None:
        client = _FakeClient(refuse_update=True)
        request = _scene()
        assert await PatchScene(request, _patches()).apply(_as_client(client)) is None
        assert client.scene.shown == [request]

    async def test_the_fallback_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _FakeClient(refuse_update=True)
        with caplog.at_level(logging.WARNING):
            await PatchScene(_scene(), _patches()).apply(_as_client(client))
        assert any("re-installing" in r.getMessage() for r in caplog.records)

    def test_summary_names_how_many_elements_moved(self) -> None:
        push = PatchScene(_scene(), _patches())
        assert push.summary == "patched 1 elements of the vox.music scene"


class TestNoPush:
    async def test_touches_the_client_not_at_all(self) -> None:
        client = _FakeClient()
        assert await NoPush("vox.music").apply(_as_client(client)) is None
        assert client.scene.shown == []
        assert client.scene.updated == []

    def test_summary_says_nothing_was_pushed(self) -> None:
        assert (
            NoPush("vox.music").summary == "vox.music scene unchanged; nothing pushed"
        )
