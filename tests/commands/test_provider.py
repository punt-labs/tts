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


def _ctx(tmp_path: Path) -> Ctx:
    """Build a Ctx with a real ConfigStore and a stub client (provider never dials)."""
    return Ctx(store=ConfigStore(tmp_path), client=MagicMock(spec_set=[]))


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

    async def test_writes_valid_provider(self, tmp_path: Path) -> None:
        result = await provider(_ctx(tmp_path), "openai")
        assert result.error is False
        assert result.json_data == {"provider": "openai"}
        assert result.text == "Provider: openai"
        assert ConfigStore(tmp_path).read().provider == "openai"

    async def test_unknown_provider_returns_error(self, tmp_path: Path) -> None:
        result = await provider(_ctx(tmp_path), "nope")
        assert result.error is True
        assert result.exit_code == 1
        assert "nope" in result.text
        assert "elevenlabs" in result.text  # allowed list surfaced

    async def test_switch_clears_stale_model(self, tmp_path: Path) -> None:
        # An elevenlabs model must not survive a switch to openai -- the model
        # namespace is provider-scoped and would be an invalid API call.
        (tmp_path / "vox.md").write_text(
            '---\nprovider: "elevenlabs"\nmodel: "eleven_v3"\n---\n'
        )
        result = await provider(_ctx(tmp_path), "openai")
        assert result.error is False
        cfg = ConfigStore(tmp_path).read()
        assert cfg.provider == "openai"
        assert cfg.model in (None, "")

    async def test_same_provider_preserves_model(self, tmp_path: Path) -> None:
        # Setting the current provider is a no-op for the model: don't clear it.
        (tmp_path / "vox.md").write_text(
            '---\nprovider: "elevenlabs"\nmodel: "eleven_v3"\n---\n'
        )
        result = await provider(_ctx(tmp_path), "elevenlabs")
        assert result.error is False
        assert ConfigStore(tmp_path).read().model == "eleven_v3"
