"""Tests for :mod:`punt_vox.lux_common.frame_id`."""

from __future__ import annotations

from punt_lux import RenderRequest
from punt_lux.operations.models import FrameSpec

from punt_vox.lux_common.frame_id import FrameId


def _request(frame: FrameSpec | None) -> RenderRequest:
    return RenderRequest(scene_id="vox.music", elements=[], frame=frame)


class TestFrameId:
    def test_an_unframed_request_resolves_to_the_scene_id(self) -> None:
        assert str(FrameId(_request(None))) == "vox.music"

    def test_a_frame_with_no_explicit_id_resolves_to_the_scene_id(self) -> None:
        frame = FrameSpec(size=(340, 340))
        assert str(FrameId(_request(frame))) == "vox.music"

    def test_a_frame_with_an_explicit_id_uses_it(self) -> None:
        frame = FrameSpec(frame_id="some.other.frame")
        assert str(FrameId(_request(frame))) == "some.other.frame"
