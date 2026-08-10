"""Tests for :mod:`punt_vox.panel.state`."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from punt_vox.panel.state import PanelState

if TYPE_CHECKING:
    from punt_vox.config import VoxConfig


@final
class _FakeStore:
    """A minimal ``ConfigStore``-shaped double: one canned ``read()`` result."""

    def __init__(self, cfg: VoxConfig) -> None:
        self._cfg = cfg

    def read(self) -> VoxConfig:
        return self._cfg


@final
class _FakeClient:
    """A minimal ``VoxClientSync``-shaped double: one canned ``voices()`` result."""

    def __init__(self, voices: list[str]) -> None:
        self._voices = voices

    def voices(self) -> list[str]:
        return self._voices


def _config(provider: str | None = None, model: str | None = None) -> VoxConfig:
    from punt_vox.config import VoxConfig

    return VoxConfig(
        notify="y",
        speak="y",
        vibe_mode="auto",
        voice="roger",
        provider=provider,
        model=model,
        vibe=None,
        vibe_tags=None,
    )


class TestEmpty:
    def test_safe_defaults(self) -> None:
        state = PanelState.empty()
        assert state.notify == "n"
        assert state.speak == "y"
        assert state.voice is None
        assert state.roster == ()
        assert state.provider is None
        assert state.model is None


class TestRead:
    def test_reads_config_and_roster(self) -> None:
        state = PanelState.read(_FakeClient(["aria", "roger"]), _FakeStore(_config()))
        assert state.notify == "y"
        assert state.speak == "y"
        assert state.voice == "roger"
        assert state.roster == ("aria", "roger")

    def test_reads_provider_and_model_from_config(self) -> None:
        state = PanelState.read(
            _FakeClient(["aria"]),
            _FakeStore(_config(provider="openai", model="tts-1")),
        )
        assert state.provider == "openai"
        assert state.model == "tts-1"


class TestWithField:
    def test_with_notify_returns_a_new_state(self) -> None:
        before = PanelState.empty()
        after = before.with_notify("c")
        assert after.notify == "c"
        assert before.notify == "n"

    def test_with_speak_returns_a_new_state(self) -> None:
        after = PanelState.empty().with_speak("n")
        assert after.speak == "n"

    def test_with_voice_returns_a_new_state(self) -> None:
        after = PanelState.empty().with_voice("aria")
        assert after.voice == "aria"

    def test_with_provider_sets_provider_and_clears_model(self) -> None:
        before = PanelState(
            notify="n",
            speak="y",
            voice=None,
            roster=(),
            provider="elevenlabs",
            model="eleven_v3",
        )
        after = before.with_provider("openai")
        assert after.provider == "openai"
        assert after.model is None
        # The old state is untouched.
        assert before.provider == "elevenlabs"
        assert before.model == "eleven_v3"

    def test_with_model_returns_a_new_state(self) -> None:
        after = PanelState.empty().with_model("tts-1")
        assert after.model == "tts-1"


class TestScene:
    def test_scene_carries_the_state_fields(self) -> None:
        state = PanelState(
            notify="c",
            speak="n",
            voice="aria",
            roster=("aria",),
            provider="openai",
            model="tts-1",
        )
        scene = state.scene()
        assert scene.notify == "c"
        assert scene.speak == "n"
        assert scene.voice == "aria"
        assert scene.roster == ("aria",)
        assert scene.provider == "openai"
        assert scene.model == "tts-1"
