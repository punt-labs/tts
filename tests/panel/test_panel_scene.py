"""Tests for :mod:`punt_vox.panel.panel_scene`."""

from __future__ import annotations

from punt_vox.panel.panel_notice import PanelNotice
from punt_vox.panel.panel_scene import PanelScene


def _scene(
    notice: PanelNotice | None = None,
    provider: str | None = "elevenlabs",
    model: str | None = "eleven_v3",
) -> PanelScene:
    """Return the reference scene, optionally carrying *notice*.

    ``None`` for *notice* means "no notice given", which the scene renders
    as its own silent default -- not a notice that failed to be produced.
    """
    return PanelScene(
        notify="y",
        speak="n",
        voice="aria",
        roster=("aria", "roger"),
        provider=provider,
        model=model,
        notice=notice if notice is not None else PanelNotice.silent(),
    )


class TestFrame:
    def test_uses_an_explicit_size_not_auto_resize(self) -> None:
        request = _scene().render_request()
        assert request.frame is not None
        assert request.frame.size is not None
        assert request.frame.flags is None

    def test_size_is_within_the_confirmed_range(self) -> None:
        request = _scene().render_request()
        assert request.frame is not None
        size = request.frame.size
        assert size is not None
        width, height = size
        assert 330 <= width <= 350
        # Height range widened after the Voice engine trio landed:
        # provider combo + model combo + section label added ~100px.
        assert 320 <= height <= 360


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

    def test_carries_the_voice_engine_trio(self) -> None:
        request = _scene().render_request()
        ids = [e["id"] for e in request.elements]
        assert "vox.panel.voice_engine" in ids
        assert "vox.panel.provider" in ids
        assert "vox.panel.model" in ids

    def test_voice_engine_section_precedes_the_provider_combo(self) -> None:
        request = _scene().render_request()
        ids = [e["id"] for e in request.elements]
        assert ids.index("vox.panel.voice_engine") < ids.index("vox.panel.provider")
        assert ids.index("vox.panel.provider") < ids.index("vox.panel.model")

    def test_provider_current_is_highlighted(self) -> None:
        request = _scene(provider="openai").render_request()
        provider_combo = next(
            e for e in request.elements if e["id"] == "vox.panel.provider"
        )
        items = provider_combo["items"]
        assert isinstance(items, list)
        assert provider_combo["selected"] == items.index("openai")

    def test_model_list_reflects_the_current_provider(self) -> None:
        # OpenAI's models: tts-1, tts-1-hd, gpt-4o-mini-tts.
        request = _scene(provider="openai", model="tts-1-hd").render_request()
        model_combo = next(e for e in request.elements if e["id"] == "vox.panel.model")
        assert model_combo["items"] == [
            "(none)",
            "tts-1",
            "tts-1-hd",
            "gpt-4o-mini-tts",
        ]
        assert model_combo["selected"] == 2

    def test_modelless_provider_renders_the_sentinel(self) -> None:
        # Polly has no user-selectable model.
        request = _scene(provider="polly", model=None).render_request()
        model_combo = next(e for e in request.elements if e["id"] == "vox.panel.model")
        assert model_combo["items"] == ["(no models)"]
        assert "handlers" not in model_combo

    def test_a_fresh_repo_claims_no_provider_and_no_voice(self) -> None:
        """The panel must not assert settings the daemon does not hold.

        A repo with nothing configured used to render "elevenlabs" as the
        selected provider and the first roster entry as the selected voice,
        neither of which was in the config. Re-picking an already-selected
        combo entry fires no ``changed`` event, so the user could not even
        confirm the claim into truth.
        """
        request = _scene(provider=None, model=None).render_request()
        provider_combo = next(
            e for e in request.elements if e["id"] == "vox.panel.provider"
        )
        items = provider_combo["items"]
        assert isinstance(items, list)
        assert items[0] == "(none)"
        assert provider_combo["selected"] == 0

    def test_missing_provider_renders_the_sentinel_and_no_handlers(self) -> None:
        """No provider chosen means no model list -- and nothing to click.

        The combo used to fall back to ElevenLabs' models here, so a session
        that had picked no provider was shown a live, clickable list
        belonging to one it never chose. Every such click was then refused
        by ``ClickTarget.model`` (which offers no models without a
        provider), logging a traceback and snapping the widget back. The
        render now agrees with the click: an inert sentinel, no handlers,
        nothing to click in the first place.
        """
        request = _scene(provider=None, model=None).render_request()
        model_combo = next(e for e in request.elements if e["id"] == "vox.panel.model")
        assert model_combo["items"] == ["(no models)"]
        assert "handlers" not in model_combo
