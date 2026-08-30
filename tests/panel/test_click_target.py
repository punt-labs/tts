"""Tests for :mod:`punt_vox.panel.click_target`."""

from __future__ import annotations

import pytest

from punt_vox.panel.click_target import ClickTarget
from punt_vox.panel.state import PanelState


def _state(
    *,
    roster: tuple[str, ...] = ("benno", "aria"),
    provider: str | None = "elevenlabs",
    model: str | None = "eleven_v3",
    voice: str | None = "benno",
) -> PanelState:
    return PanelState(
        notify="y",
        speak="y",
        voice=voice,
        roster=roster,
        provider=provider,
        model=model,
    )


class TestVoice:
    def test_index_names_the_roster_entry_at_that_position(self) -> None:
        assert ClickTarget(_state()).voice(2) == "aria"

    def test_an_index_past_the_roster_is_refused(self) -> None:
        with pytest.raises(ValueError):
            ClickTarget(_state()).voice(7)

    def test_an_empty_roster_refuses_every_index(self) -> None:
        with pytest.raises(ValueError):
            ClickTarget(_state(roster=(), voice=None)).voice(0)


class TestProvider:
    def test_index_names_the_provider_at_that_position(self) -> None:
        # The provider list is closed and ordered: elevenlabs, openai,
        # polly, say, espeak.
        assert ClickTarget(_state()).provider(1) == "elevenlabs"
        assert ClickTarget(_state()).provider(5) == "espeak"

    def test_an_index_past_the_provider_list_is_refused(self) -> None:
        with pytest.raises(ValueError):
            ClickTarget(_state()).provider(9)


class TestModel:
    def test_index_names_a_model_of_the_snapshot_s_provider(self) -> None:
        assert ClickTarget(_state()).model(1) == "eleven_v3"

    def test_a_snapshot_with_no_provider_offers_no_models(self) -> None:
        """No provider chosen means no model list -- never a substituted one.

        The panel used to fall back to ElevenLabs' models here, showing a
        session that had picked no provider a list belonging to one it
        never chose. Every index must now be refused instead.
        """
        target = ClickTarget(_state(provider=None, model=None))
        with pytest.raises(ValueError):
            target.model(0)

    def test_a_modelless_provider_refuses_every_index(self) -> None:
        target = ClickTarget(_state(provider="say", model=None))
        with pytest.raises(ValueError):
            target.model(0)
