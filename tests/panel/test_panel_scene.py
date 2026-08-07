"""Tests for :mod:`punt_vox.panel.panel_scene`."""

from __future__ import annotations

from punt_vox.panel.panel_notice import PanelNotice
from punt_vox.panel.panel_scene import PanelScene


def _scene(**overrides: object) -> PanelScene:
    fields: dict[str, object] = {
        "notify": "y",
        "speak": "n",
        "voice": "aria",
        "roster": ("aria", "roger"),
    }
    fields.update(overrides)
    return PanelScene(**fields)  # type: ignore[arg-type]


class TestFrame:
    def test_uses_an_explicit_size_not_auto_resize(self) -> None:
        request = _scene().render_request()
        assert request.frame is not None
        assert request.frame.size is not None
        assert request.frame.flags is None

    def test_size_is_within_the_confirmed_range(self) -> None:
        request = _scene().render_request()
        assert request.frame is not None
        width, height = request.frame.size  # type: ignore[misc]
        assert 330 <= width <= 350
        assert 220 <= height <= 260


class TestStatusLine:
    def test_silent_notice_renders_an_empty_status_line(self) -> None:
        request = _scene().render_request()
        status = request.elements[0]
        assert status["id"] == "vox.panel.status"
        assert status["content"] == ""

    def test_warning_notice_renders_its_message(self) -> None:
        notice = PanelNotice.voxd_unavailable()
        request = _scene(notice=notice).render_request()
        status = request.elements[0]
        assert status["content"] == notice.message

    def test_scene_shape_is_unchanged_between_silent_and_warning(self) -> None:
        silent = _scene().render_request()
        warning = _scene(notice=PanelNotice.voxd_unavailable()).render_request()
        assert len(silent.elements) == len(warning.elements)
        assert [e["id"] for e in silent.elements] == [e["id"] for e in warning.elements]


class TestElements:
    def test_carries_both_radios_and_the_voice_row(self) -> None:
        request = _scene().render_request()
        ids = [e["id"] for e in request.elements]
        assert "vox.panel.notify" in ids
        assert "vox.panel.mic_mode" in ids
        assert "vox.panel.voice.row" in ids
