"""Tests for :mod:`punt_vox.panel.panel_push`.

Two properties matter. The first is the one the user sees: a refresh must not
reinstall, because installing raises the frame -- everything else here is what
has to hold for that to stay safe (a refusal or an outage leaves the panel's
idea of what is installed *disarmed*, so the next push installs rather than
patching a tree luxd never accepted). The second is the DES-072 addendum:
``show`` alone does not reliably raise a frame the scene is not new to, so
:meth:`PanelPush.install` must ALSO make an explicit ``client.frame.raise_``
call once the scene push lands -- and it must do so even on the second and
later clicks, when the panel scene is already installed and ``show``'s own
raise would (per the bug) stay silent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast, final

import pytest
from punt_lux import HubUnavailableError, LuxClient, OpError, RenderRequest, SceneShown
from punt_lux.operations import FrameRaise

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
class _FakeFrameAccessor:
    """Records every ``raise_`` call, answering each with a successful raise."""

    def __init__(self) -> None:
        self.raised: list[str] = []

    async def raise_(self, frame_id: str) -> FrameRaise | OpError:
        self.raised.append(frame_id)
        return FrameRaise(frame_id=frame_id, raised=True)


@final
class _FakeClient:
    def __init__(self, *, refuse: bool = False, down: bool = False) -> None:
        self.scene = _FakeSceneAccessor(refuse=refuse, down=down)
        self.frame = _FakeFrameAccessor()


@final
class _GatedSceneAccessor:
    """Records every call in order and holds the first one open until released.

    The gate is what makes the interleaving deterministic: it parks one push
    mid-flight, exactly in the window between its plan and its confirmation,
    which is the window a second push must not be able to see through.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.patched: list[list[dict[str, object]]] = []
        self.released = asyncio.Event()
        self.first_call_started = asyncio.Event()

    async def show(self, request: RenderRequest) -> SceneShown | OpError:
        self.calls.append("show")
        await self._gate()
        return SceneShown(scene_id=request.scene_id)

    async def update(
        self, scene_id: str, request: UpdateRequest | OpError
    ) -> SceneShown | OpError:
        self.calls.append("update")
        assert not isinstance(request, OpError)
        self.patched.append(request.to_wire())
        await self._gate()
        return SceneShown(scene_id=scene_id)

    async def _gate(self) -> None:
        if self.first_call_started.is_set():
            return
        self.first_call_started.set()
        await self.released.wait()


@final
class _GatedClient:
    def __init__(self) -> None:
        self.scene = _GatedSceneAccessor()


def _as_client(fake: _FakeClient | _GatedClient) -> LuxClient:
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
        # DES-072 addendum: ``show`` alone does not reliably raise a frame the
        # scene is not new to -- the install must ALSO make an explicit raise.
        assert client.frame.raised == ["vox.panel"]


class TestExplicitFrameRaise:
    """DES-072 addendum: ``show`` only raises a frame the scene is new to.

    A ``Vox`` menu click's :meth:`PanelPush.install` must make its own explicit
    ``client.frame.raise_`` call after the push lands -- and, crucially, the
    raise must fire on the second and later clicks, when the panel scene is
    already known to luxd and ``show``'s own raise would (per the bug) stay
    silent.
    """

    async def test_raises_even_when_the_scene_was_not_new_to_luxd(self) -> None:
        push, client = PanelPush(), _FakeClient()
        await push.install(_as_client(client), _scene())  # first: becomes "known"
        await push.install(_as_client(client), _scene())  # a later click
        await push.install(_as_client(client), _scene())  # and again -- still not new

        assert client.frame.raised == ["vox.panel", "vox.panel", "vox.panel"]

    async def test_a_refresh_never_raises_the_frame(self) -> None:
        push, client = PanelPush(), _FakeClient()
        await push.refresh(_as_client(client), _scene())
        await push.refresh(_as_client(client), _scene("changed"))

        assert client.frame.raised == []

    async def test_a_refused_install_skips_the_raise(self) -> None:
        push, client = PanelPush(), _FakeClient(refuse=True)
        await push.install(_as_client(client), _scene())

        assert client.frame.raised == []

    async def test_an_absent_luxd_on_install_skips_the_raise(self) -> None:
        push, client = PanelPush(), _FakeClient(down=True)
        with pytest.raises(HubUnavailableError):
            await push.install(_as_client(client), _scene())

        assert client.frame.raised == []

    async def test_a_refused_raise_is_logged_and_swallowed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        @final
        class _RefusingFrameAccessor:
            async def raise_(self, frame_id: str) -> FrameRaise | OpError:
                return OpError(code="rejected", reason="no such frame")

        client = _FakeClient()
        client.frame = _RefusingFrameAccessor()  # type: ignore[assignment]
        with caplog.at_level(logging.WARNING):
            await PanelPush().install(_as_client(client), _scene())

        assert any(
            "vox-panel" in r.getMessage() and "refused to raise" in r.getMessage()
            for r in caplog.records
        )

    async def test_an_unreachable_raise_is_logged_and_swallowed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        @final
        class _DownFrameAccessor:
            async def raise_(self, frame_id: str) -> FrameRaise | OpError:
                raise HubUnavailableError("luxd is not running")

        client = _FakeClient()
        client.frame = _DownFrameAccessor()  # type: ignore[assignment]
        with caplog.at_level(logging.WARNING):
            await PanelPush().install(_as_client(client), _scene())

        assert any(
            "vox-panel" in r.getMessage() and "unavailable" in r.getMessage()
            for r in caplog.records
        )


class TestConcurrentPushes:
    """The panel's leg spawns a bare task per click and per control event.

    Nothing orders those tasks, so two pushes can be in flight at once. Planning
    a push claims a render as installed and applying it is a separate await; a
    second push that plans inside that window diffs against a tree luxd has not
    got yet, and if the two land out of order the screen and this object disagree
    permanently -- every later diff skips the fields they disagree about.
    """

    async def test_a_second_push_cannot_plan_while_the_first_is_in_flight(
        self,
    ) -> None:
        push, client = PanelPush(), _GatedClient()

        first = asyncio.create_task(push.refresh(_as_client(client), _scene("first")))
        await asyncio.wait_for(client.scene.first_call_started.wait(), timeout=1.0)

        second = asyncio.create_task(push.refresh(_as_client(client), _scene("second")))
        await asyncio.sleep(0.05)  # ample time for an unserialized push to run

        # The whole assertion: while the install is still in flight, the second
        # push has touched the client not at all. Unserialized, it would already
        # have sent an `update` diffed against a scene luxd has never seen.
        assert client.scene.calls == ["show"]

        client.scene.released.set()
        await asyncio.gather(first, second)

    async def test_the_second_push_patches_against_what_actually_landed(
        self,
    ) -> None:
        push, client = PanelPush(), _GatedClient()

        first = asyncio.create_task(push.refresh(_as_client(client), _scene("first")))
        await asyncio.wait_for(client.scene.first_call_started.wait(), timeout=1.0)
        second = asyncio.create_task(push.refresh(_as_client(client), _scene("second")))
        client.scene.released.set()
        await asyncio.gather(first, second)

        # Install then patch, in that order, with the patch carrying the field
        # that actually differs from the render that landed.
        assert client.scene.calls == ["show", "update"]
        assert client.scene.patched == [
            [{"id": "vox.panel.status", "set": {"content": "second"}}]
        ]


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
