"""Tests for :obj:`punt_vox.commands.provider` -- the pure command function.

Exercised directly through a real :class:`~punt_vox.config.ConfigStore` on
a tmp dir; no CLI runner, no MCP round-trip, no daemon.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

from punt_vox.commands import Ctx, provider
from punt_vox.config import ConfigStore
from punt_vox.server_switches import PROVIDER_NAMES


def _ctx(tmp_path: Path, voices: list[str] | None = None) -> Ctx:
    """Build a Ctx with a real ConfigStore and a voxd client stub for cascade.

    The cascade rule fetches the new provider's voice roster on every
    genuine set; tests supply what that roster returns via *voices*.
    """
    client = MagicMock()
    client.voices.return_value = voices if voices is not None else []
    return Ctx(store=ConfigStore(tmp_path), client=client)


class TestList:
    """No name given -- list the closed provider enum."""

    async def test_lists_all_five_providers(self, tmp_path: Path) -> None:
        result = await provider(_ctx(tmp_path), None)
        assert result.error is False
        assert result.json_data is not None
        names = cast("list[str]", result.json_data["names"])
        assert names == list(PROVIDER_NAMES)

    async def test_marks_current(self, tmp_path: Path) -> None:
        (tmp_path / "vox.md").write_text('---\nprovider: "polly"\n---\n')
        result = await provider(_ctx(tmp_path), None)
        assert "polly (current)" in result.text
        assert result.json_data is not None
        assert result.json_data["current"] == "polly"

    async def test_no_current_when_unset(self, tmp_path: Path) -> None:
        result = await provider(_ctx(tmp_path), None)
        assert result.json_data is not None
        assert result.json_data["current"] is None


class TestSet:
    """A name given -- validate and write to config."""

    async def test_writes_valid_provider_and_cascades_defaults(
        self, tmp_path: Path
    ) -> None:
        """The cascade rule (vox-awm9): setting provider populates model + voice.

        openai's first model is ``tts-1``; the fake roster's first voice is
        ``alloy``. Both land in state and in the result payload.
        """
        result = await provider(_ctx(tmp_path, voices=["alloy", "nova"]), "openai")
        assert result.error is False
        assert result.json_data == {
            "provider": "openai",
            "model": "tts-1",
            "voice": "alloy",
        }
        assert result.text == "Provider: openai"
        cfg = ConfigStore(tmp_path).read()
        assert cfg.provider == "openai"
        assert cfg.model == "tts-1"
        assert cfg.voice == "alloy"

    async def test_modelless_provider_cascades_voice_leaves_model_empty(
        self, tmp_path: Path
    ) -> None:
        """A modelless provider (polly/say/espeak) still cascades voice."""
        result = await provider(_ctx(tmp_path, voices=["joanna", "matthew"]), "polly")
        assert result.error is False
        assert result.json_data == {
            "provider": "polly",
            "model": "",
            "voice": "joanna",
        }
        cfg = ConfigStore(tmp_path).read()
        assert cfg.model in (None, "")
        assert cfg.voice == "joanna"

    async def test_unknown_provider_returns_error(self, tmp_path: Path) -> None:
        result = await provider(_ctx(tmp_path), "nope")
        assert result.error is True
        assert result.exit_code == 1
        assert "nope" in result.text
        assert "elevenlabs" in result.text  # allowed list surfaced

    async def test_switch_overwrites_model_and_voice_with_defaults(
        self, tmp_path: Path
    ) -> None:
        """A genuine switch replaces model + voice with the new provider's defaults.

        Was: 'clears stale model' (vox-0rp9.1). Under vox-awm9 the rule
        replaces both fields with deterministic first-from-list defaults.
        """
        (tmp_path / "vox.md").write_text(
            '---\nprovider: "elevenlabs"\nmodel: "eleven_v3"\nvoice: "matilda"\n---\n'
        )
        result = await provider(_ctx(tmp_path, voices=["alloy", "nova"]), "openai")
        assert result.error is False
        cfg = ConfigStore(tmp_path).read()
        assert cfg.provider == "openai"
        assert cfg.model == "tts-1"
        assert cfg.voice == "alloy"

    async def test_same_provider_is_a_no_op_no_cascade(self, tmp_path: Path) -> None:
        """Re-publishing the same provider is a no-op; model + voice preserved."""
        (tmp_path / "vox.md").write_text(
            '---\nprovider: "elevenlabs"\nmodel: "eleven_v3"\nvoice: "matilda"\n---\n'
        )
        result = await provider(_ctx(tmp_path, voices=["roger"]), "elevenlabs")
        assert result.error is False
        # No-op returns just the provider; no cascade fires.
        assert result.json_data == {"provider": "elevenlabs"}
        cfg = ConfigStore(tmp_path).read()
        assert cfg.model == "eleven_v3"
        assert cfg.voice == "matilda"

    async def test_roster_fetch_error_aborts_the_write(self, tmp_path: Path) -> None:
        """A daemon fault on the cascade roster fetch returns an error envelope.

        Same rule as vox-w79f on the panel: the provider MUST NOT persist
        when we cannot read the voice roster to compute the cascaded default.
        """
        from punt_vox.client_errors import VoxdConnectionError

        client = MagicMock()
        client.voices.side_effect = VoxdConnectionError("voxd unreachable")
        ctx = Ctx(store=ConfigStore(tmp_path), client=client)

        result = await provider(ctx, "openai")

        assert result.error is True
        assert result.exit_code == 1
        # Original state is intact -- the failed switch left nothing changed.
        cfg = ConfigStore(tmp_path).read()
        assert cfg.provider is None
