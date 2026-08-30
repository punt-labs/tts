"""Tests for :mod:`punt_vox.lux_common.frame_raise`.

Shared by :class:`~punt_vox.voxd.music_player.lux_scene_publisher.LuxScenePublisher`
and :class:`~punt_vox.panel.panel_push.PanelPush`, so its own tests exercise it
directly rather than through either caller: resolve the frame id, call
``client.frame.raise_``, and report -- never raise -- on a refusal, an absent
luxd, or a frame the display does not hold.
"""

from __future__ import annotations

from typing import cast, final

from punt_lux import HubUnavailableError, LuxClient, OpError, RenderRequest
from punt_lux.operations import FrameRaise

from punt_vox.lux_common.frame_raise import FrameRaiser


def _request(scene_id: str = "vox.music") -> RenderRequest:
    return RenderRequest(scene_id=scene_id, elements=[])


@final
class _FakeFrameAccessor:
    def __init__(self) -> None:
        self.raised: list[str] = []

    async def raise_(self, frame_id: str) -> FrameRaise | OpError:
        self.raised.append(frame_id)
        return FrameRaise(frame_id=frame_id, raised=True)


@final
class _RefusingFrameAccessor:
    async def raise_(self, frame_id: str) -> FrameRaise | OpError:
        return OpError(code="rejected", reason="no such frame")


@final
class _DownFrameAccessor:
    async def raise_(self, frame_id: str) -> FrameRaise | OpError:
        raise HubUnavailableError("luxd is not running")


@final
class _AbsentFrameAccessor:
    async def raise_(self, frame_id: str) -> FrameRaise | OpError:
        return FrameRaise(frame_id=frame_id, raised=False)


def _client(frame: object) -> LuxClient:
    return cast("LuxClient", type("_C", (), {"frame": frame})())


async def test_raises_the_requests_resolved_frame_id() -> None:
    frame = _FakeFrameAccessor()
    warnings: list[str] = []

    await FrameRaiser(warnings.append).raise_frame(_client(frame), _request())

    assert frame.raised == ["vox.music"]
    assert warnings == []


async def test_a_refusal_is_reported_via_warn_not_raised() -> None:
    warnings: list[str] = []

    await FrameRaiser(warnings.append).raise_frame(
        _client(_RefusingFrameAccessor()), _request()
    )

    assert any("refused to raise" in w for w in warnings)


async def test_an_unreachable_luxd_is_reported_via_warn_not_raised() -> None:
    warnings: list[str] = []

    await FrameRaiser(warnings.append).raise_frame(
        _client(_DownFrameAccessor()), _request()
    )

    assert any("unavailable" in w for w in warnings)


async def test_a_frame_the_display_does_not_hold_is_reported() -> None:
    warnings: list[str] = []

    await FrameRaiser(warnings.append).raise_frame(
        _client(_AbsentFrameAccessor()), _request()
    )

    assert any("holds no" in w for w in warnings)
