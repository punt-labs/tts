"""Tests for LuxScenePublisher: async render, down/slow luxd never blocks.

Three mandatory properties. The first (design 3.2): a slow or unreachable luxd
must not stall the caller or the event loop, and a lux failure is logged and
dropped, never raised into audio control. The second is vox-h777's: a *refresh*
must not reinstall the scene, because installing raises the frame -- so a track
change patches the installed tree and leaves the window exactly where the user
put it, while a menu click still shows and brings it forward. The third is the
DES-072 addendum: ``show`` alone does not reliably raise/unminimize a frame
already installed, so an install-intent delivery must ALSO make an explicit
``client.frame.raise_`` call -- and it must do so even when the scene was not
new to luxd, which is exactly the case a second-and-later menu click hits.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast, final

from punt_lux import HubUnavailableError, LuxClient, OpError, RenderRequest, SceneShown
from punt_lux.operations import FrameRaise

from punt_vox.voxd.music_player.lux_scene_publisher import LuxScenePublisher

if TYPE_CHECKING:
    import pytest
    from punt_lux.operations import UpdateRequest


def _scene(scene_id: str, content: str = "") -> RenderRequest:
    return RenderRequest(
        scene_id=scene_id,
        elements=[{"kind": "text", "id": "music.status", "content": content}],
        title="Music",
    )


@final
class _FakeSceneAccessor:
    """Records every show and every update, answering both with success."""

    def __init__(self) -> None:
        self.rendered: list[RenderRequest] = []
        self.patched: list[list[dict[str, object]]] = []

    async def show(self, request: RenderRequest) -> SceneShown | OpError:
        self.rendered.append(request)
        return SceneShown(scene_id=request.scene_id)

    async def update(
        self, scene_id: str, request: UpdateRequest | OpError
    ) -> SceneShown | OpError:
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
class _UnexpectedFrameAccessor:
    """A ``frame`` stand-in that raises if it is ever called -- fixture-only guard.

    Not a substitute for an assertion: :meth:`LuxScenePublisher.run`'s own
    ``except Exception`` boundary would catch and silently swallow an
    ``AssertionError`` raised from here just as it would any other exception,
    so a test whose only check IS "no exception escaped" proves nothing (see
    ``TestExplicitFrameRaise.test_a_refused_install_push_skips_the_raise`` and
    its sibling, fixed to assert ``client.frame.raised == []`` against a
    recording ``_FakeFrameAccessor`` instead). Retained only where no test
    presently asserts on frame-raise behavior through this fixture at all.
    """

    async def raise_(self, frame_id: str) -> FrameRaise | OpError:
        msg = f"client.frame.raise_({frame_id!r}) should not have been called here"
        raise AssertionError(msg)


@final
class _FakeClient:
    """A LuxClient stand-in exposing the ``scene`` and ``frame`` accessors."""

    def __init__(self) -> None:
        self.scene = _FakeSceneAccessor()
        self.frame = _FakeFrameAccessor()


@final
class _DownSceneAccessor:
    async def show(self, request: RenderRequest) -> SceneShown | OpError:
        raise HubUnavailableError("luxd is not running")

    async def update(
        self, scene_id: str, request: UpdateRequest | OpError
    ) -> SceneShown | OpError:
        raise HubUnavailableError("luxd is not running")


@final
class _DownClient:
    """Always unreachable -- what a stopped luxd looks like."""

    def __init__(self) -> None:
        self.scene = _DownSceneAccessor()
        # A down luxd fails inside ``show``/``update`` themselves, so the
        # publisher never reaches the raise step. A recording double, not an
        # asserting one: ``run()``'s own ``except Exception`` boundary would
        # catch and swallow an ``AssertionError`` raised from here just like
        # any other exception, silently defeating the check -- the test must
        # assert on ``raised == []`` instead of relying on one never firing.
        self.frame = _FakeFrameAccessor()


@final
class _RejectingSceneAccessor:
    async def show(self, request: RenderRequest) -> SceneShown | OpError:
        return OpError(code="rejected", reason="scene refused")

    async def update(
        self, scene_id: str, request: UpdateRequest | OpError
    ) -> SceneShown | OpError:
        return OpError(code="rejected", reason="scene refused")


@final
class _RejectingClient:
    def __init__(self) -> None:
        self.scene = _RejectingSceneAccessor()
        # A refused scene push must not be followed by a raise attempt. A
        # recording double, not an asserting one: ``run()``'s own ``except
        # Exception`` boundary would catch and swallow an ``AssertionError``
        # raised from here just like any other exception, silently defeating
        # the check -- the test must assert on ``raised == []`` instead of
        # relying on one never firing.
        self.frame = _FakeFrameAccessor()


@final
class _RestartedSceneAccessor:
    """Refuses the first update, as a restarted luxd refuses an unknown scene."""

    def __init__(self) -> None:
        self.rendered: list[RenderRequest] = []
        self.updates = 0

    async def show(self, request: RenderRequest) -> SceneShown | OpError:
        self.rendered.append(request)
        return SceneShown(scene_id=request.scene_id)

    async def update(
        self, scene_id: str, request: UpdateRequest | OpError
    ) -> SceneShown | OpError:
        self.updates += 1
        return OpError(code="not_found", reason="no such scene")


@final
class _RestartedClient:
    def __init__(self) -> None:
        self.scene = _RestartedSceneAccessor()
        # Every push in this scenario is a plain ``submit``, never a
        # ``reinstall``; a raise attempt here would be a regression.
        self.frame = _UnexpectedFrameAccessor()


@final
class _BlockingSceneAccessor:
    """Blocks for a long time inside show -- a slow luxd."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def show(self, request: RenderRequest) -> SceneShown | OpError:
        self.started.set()
        await asyncio.sleep(5.0)  # slow, but async -- the loop keeps ticking
        return SceneShown(scene_id=request.scene_id)


@final
class _BlockingClient:
    def __init__(self) -> None:
        self.scene = _BlockingSceneAccessor()


def _as_client(fake: object) -> LuxClient:
    """Cast a duck-typed publisher stand-in to the LuxClient type the seam wants."""
    return cast("LuxClient", fake)


async def _drain_once(publisher: LuxScenePublisher, *, settle: float = 0.1) -> None:
    task = asyncio.create_task(publisher.run())
    await asyncio.sleep(settle)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_run_renders_the_submitted_scene() -> None:
    client = _FakeClient()
    publisher = LuxScenePublisher(lambda: _as_client(client))
    publisher.submit(_scene("vox.music"))
    await _drain_once(publisher)
    assert [r.scene_id for r in client.scene.rendered] == ["vox.music"]


async def test_run_logs_the_push_with_element_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _FakeClient()
    publisher = LuxScenePublisher(lambda: _as_client(client))
    publisher.submit(_scene("vox.music"))
    with caplog.at_level(logging.INFO):
        await _drain_once(publisher)
    pushed = [
        r
        for r in caplog.records
        if "[lux]" in r.getMessage() and "installed vox.music scene" in r.getMessage()
    ]
    assert pushed
    assert "1 elements" in pushed[-1].getMessage()


async def test_submit_neither_connects_nor_renders() -> None:
    connected = False

    def _connect() -> LuxClient:
        nonlocal connected
        connected = True
        return _as_client(_FakeClient())

    LuxScenePublisher(_connect).submit(_scene("vox.music"))  # no run() -> no drain
    assert connected is False  # submit only touches the mailbox


class TestRefreshDoesNotReinstall:
    async def test_a_changed_value_patches_and_never_shows_again(self) -> None:
        # The reported defect: a track change used to re-install the whole tree,
        # which raises the frame and yanks the window in front of whatever the
        # user had put on top of it.
        client = _FakeClient()
        publisher = LuxScenePublisher(lambda: _as_client(client))
        task = asyncio.create_task(publisher.run())

        publisher.submit(_scene("vox.music", "1 of 12"))
        await asyncio.sleep(0.05)
        publisher.submit(_scene("vox.music", "2 of 12"))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert len(client.scene.rendered) == 1  # the install, and only the install
        assert client.scene.patched == [
            [{"id": "music.status", "set": {"content": "2 of 12"}}]
        ]

    async def test_an_identical_render_puts_nothing_on_the_wire(self) -> None:
        client = _FakeClient()
        publisher = LuxScenePublisher(lambda: _as_client(client))
        task = asyncio.create_task(publisher.run())

        publisher.submit(_scene("vox.music", "same"))
        await asyncio.sleep(0.05)
        publisher.submit(_scene("vox.music", "same"))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert len(client.scene.rendered) == 1
        assert client.scene.patched == []

    async def test_reinstall_shows_even_when_nothing_changed(self) -> None:
        # The Music menu entry's whole job: bring the window forward. It must keep
        # showing, identical render or not.
        client = _FakeClient()
        publisher = LuxScenePublisher(lambda: _as_client(client))
        task = asyncio.create_task(publisher.run())

        publisher.submit(_scene("vox.music", "same"))
        await asyncio.sleep(0.05)
        publisher.reinstall(_scene("vox.music", "same"))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert len(client.scene.rendered) == 2
        # DES-072 addendum: ``show`` alone does not reliably raise a frame the
        # scene is not new to -- the reinstall must ALSO make an explicit raise.
        assert client.frame.raised == ["vox.music"]


class TestExplicitFrameRaise:
    """DES-072 addendum: ``show`` only raises a frame the scene is new to.

    A menu click's ``reinstall`` must make its own explicit
    ``client.frame.raise_`` call after the push lands -- and, crucially, the
    raise must fire on the second and later clicks, when the scene is already
    known to luxd and ``show``'s own raise would (per the bug) stay silent.
    """

    async def test_raises_even_when_the_scene_was_not_new_to_luxd(self) -> None:
        client = _FakeClient()
        publisher = LuxScenePublisher(lambda: _as_client(client))
        task = asyncio.create_task(publisher.run())

        publisher.submit(_scene("vox.music"))  # first: the scene becomes "known"
        await asyncio.sleep(0.05)
        publisher.reinstall(_scene("vox.music"))  # a later menu click, re-clicked
        await asyncio.sleep(0.05)
        publisher.reinstall(_scene("vox.music"))  # and again -- still not new
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert client.frame.raised == ["vox.music", "vox.music"]

    async def test_a_plain_refresh_never_raises_the_frame(self) -> None:
        client = _FakeClient()
        publisher = LuxScenePublisher(lambda: _as_client(client))
        task = asyncio.create_task(publisher.run())

        publisher.submit(_scene("vox.music", "1 of 12"))
        await asyncio.sleep(0.05)
        publisher.submit(_scene("vox.music", "2 of 12"))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert client.frame.raised == []

    async def test_a_refused_install_push_skips_the_raise(self) -> None:
        client = _RejectingClient()
        publisher = LuxScenePublisher(lambda: _as_client(client))
        publisher.reinstall(_scene("vox.music"))
        await _drain_once(publisher)

        assert client.frame.raised == []

    async def test_a_down_luxd_on_the_scene_push_skips_the_raise(self) -> None:
        client = _DownClient()
        publisher = LuxScenePublisher(lambda: _as_client(client))
        publisher.reinstall(_scene("vox.music"))
        await _drain_once(publisher)

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
        publisher = LuxScenePublisher(lambda: _as_client(client))
        publisher.reinstall(_scene("vox.music"))
        with caplog.at_level(logging.WARNING):
            await _drain_once(publisher)

        assert any(
            "[lux]" in r.getMessage() and "refused to raise" in r.getMessage()
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
        publisher = LuxScenePublisher(lambda: _as_client(client))
        publisher.reinstall(_scene("vox.music"))
        with caplog.at_level(logging.WARNING):
            await _drain_once(publisher)

        assert any(
            "[lux]" in r.getMessage() and "unavailable" in r.getMessage()
            for r in caplog.records
        )


async def test_a_down_lux_is_dropped_then_reinstalls_on_the_new_connection() -> None:
    fake = _FakeClient()
    sequence: list[LuxClient] = [_as_client(_DownClient()), _as_client(fake)]
    clients = iter(sequence)
    publisher = LuxScenePublisher(lambda: next(clients))

    task = asyncio.create_task(publisher.run())
    publisher.submit(_scene("first"))
    await asyncio.sleep(0.1)  # drain 1: down -> dropped, client and scene reset
    publisher.submit(_scene("second"))
    await asyncio.sleep(0.1)  # drain 2: reconnect -> installed, never patched
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert [r.scene_id for r in fake.scene.rendered] == ["second"]
    assert fake.scene.patched == []  # the fresh luxd holds nothing to patch


async def test_an_op_error_is_logged_at_error_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    publisher = LuxScenePublisher(lambda: _as_client(_RejectingClient()))
    publisher.submit(_scene("vox.music"))
    with caplog.at_level(logging.WARNING):
        await _drain_once(publisher)
    rejected = [
        r
        for r in caplog.records
        if "[lux]" in r.getMessage() and "rejected" in r.getMessage()
    ]
    assert rejected  # logged, never raised
    # A refused scene is a defect, not a down display: it reads at ERROR, distinct
    # from the WARNING a HubUnavailableError (luxd down) logs.
    assert all(r.levelno == logging.ERROR for r in rejected)


async def test_a_restarted_luxd_recovers_through_the_patch_refusal() -> None:
    # luxd restarted while the publisher's REST client never errored, so it still
    # believes its scene is installed. The first patch meets an unknown scene, is
    # rejected whole (nothing mutated), and the fallback installs.
    client = _RestartedClient()
    publisher = LuxScenePublisher(lambda: _as_client(client))
    task = asyncio.create_task(publisher.run())

    publisher.submit(_scene("vox.music", "1 of 12"))
    await asyncio.sleep(0.05)
    publisher.submit(_scene("vox.music", "2 of 12"))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert client.scene.updates == 1  # one wasted round-trip
    assert [r.elements[0]["content"] for r in client.scene.rendered] == [
        "1 of 12",
        "2 of 12",
    ]


async def test_an_unexpected_error_disarms_the_live_scene() -> None:
    # The publisher's run() loop only catches HubUnavailableError and a scene
    # refusal inside _publish -- an unrelated bug surfacing from push.apply
    # must still disarm the live scene, so the next push installs afresh
    # rather than patch against a tree that may never have landed. Proved
    # observably: a third submit for the same scene must show() again, not
    # update() -- if disarm had not run, it would patch instead.
    @final
    class _BoomOnceOnUpdateSceneAccessor:
        def __init__(self) -> None:
            self.shown = 0
            self.boom_next_update = False

        async def show(self, request: RenderRequest) -> SceneShown | OpError:
            self.shown += 1
            return SceneShown(scene_id=request.scene_id)

        async def update(
            self, scene_id: str, request: UpdateRequest | OpError
        ) -> SceneShown | OpError:
            if self.boom_next_update:
                self.boom_next_update = False
                msg = "boom"
                raise RuntimeError(msg)
            return SceneShown(scene_id=scene_id)

    @final
    class _BoomOnceOnUpdateClient:
        def __init__(self) -> None:
            self.scene = _BoomOnceOnUpdateSceneAccessor()
            self.frame = _FakeFrameAccessor()

    client = _BoomOnceOnUpdateClient()
    publisher = LuxScenePublisher(lambda: _as_client(client))
    task = asyncio.create_task(publisher.run())

    publisher.submit(_scene("vox.music", "1 of 12"))
    await asyncio.sleep(0.05)
    assert client.scene.shown == 1  # the first push installed cleanly

    client.scene.boom_next_update = True
    publisher.submit(_scene("vox.music", "2 of 12"))  # patch path -> update() raises
    await asyncio.sleep(0.05)

    # Disarmed by the unexpected failure: the next push must install again
    # rather than patch against a tree that may never have landed.
    publisher.submit(_scene("vox.music", "3 of 12"))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert client.scene.shown == 2  # the original install, plus the recovery install


async def test_a_slow_render_does_not_block_the_event_loop() -> None:
    client = _BlockingClient()
    publisher = LuxScenePublisher(lambda: _as_client(client))
    publisher.submit(_scene("vox.music"))
    task = asyncio.create_task(publisher.run())

    await asyncio.wait_for(client.scene.started.wait(), timeout=1.0)
    # The render is now awaiting a 5s sleep; the event loop must keep ticking --
    # five quick sleeps complete while the slow render is still stuck.
    ticks = 0
    for _ in range(5):
        await asyncio.sleep(0.01)
        ticks += 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert ticks == 5
