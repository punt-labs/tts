"""Tests for :func:`punt_vox.commands.model` -- the pure command function.

The command function is exercised directly through a real
:class:`~punt_vox.config.ConfigStore` on a tmp dir; no CLI runner, no MCP
round-trip, no daemon.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

from punt_vox.commands import Ctx, model
from punt_vox.config import ConfigStore


def _ctx(tmp_path: Path) -> Ctx:
    """Build a Ctx with a real ConfigStore and a stub client (model never dials)."""
    return Ctx(store=ConfigStore(tmp_path), client=MagicMock(spec_set=[]))


class TestList:
    """No name given -- list the current provider's models."""

    async def test_default_provider_lists_elevenlabs_models(
        self, tmp_path: Path
    ) -> None:
        result = await model(_ctx(tmp_path), None)
        assert result.error is False
        assert "eleven_v3" in result.text
        assert result.json_data is not None
        names = cast("list[str]", result.json_data["names"])
        assert "eleven_v3" in names

    async def test_marks_current(self, tmp_path: Path) -> None:
        (tmp_path / "vox.md").write_text('---\nmodel: "eleven_flash_v2_5"\n---\n')
        result = await model(_ctx(tmp_path), None)
        assert "eleven_flash_v2_5 (current)" in result.text
        assert result.json_data is not None
        assert result.json_data["current"] == "eleven_flash_v2_5"

    async def test_modelless_provider_uses_empty_message(self, tmp_path: Path) -> None:
        (tmp_path / "vox.md").write_text('---\nprovider: "polly"\n---\n')
        result = await model(_ctx(tmp_path), None)
        assert result.text == "No models for this provider."
        assert result.json_data == {"names": [], "current": None}


class TestSet:
    """A name given -- resolve shorthand and write the full model name."""

    async def test_shorthand_resolves_and_writes(self, tmp_path: Path) -> None:
        result = await model(_ctx(tmp_path), "v3")
        assert result.error is False
        assert result.json_data == {"model": "eleven_v3"}
        assert result.text == "Model: eleven_v3"
        # Full round-trip: reading through ConfigStore sees the written value.
        assert ConfigStore(tmp_path).read().model == "eleven_v3"

    async def test_full_name_is_accepted(self, tmp_path: Path) -> None:
        result = await model(_ctx(tmp_path), "eleven_turbo_v2_5")
        assert result.error is False
        assert result.json_data == {"model": "eleven_turbo_v2_5"}

    async def test_unknown_model_returns_error(self, tmp_path: Path) -> None:
        result = await model(_ctx(tmp_path), "does-not-exist")
        assert result.error is True
        assert result.exit_code == 1
        assert "does-not-exist" in result.text
        assert result.json_data is not None
        assert "error" in result.json_data

    async def test_modelless_provider_returns_error(self, tmp_path: Path) -> None:
        (tmp_path / "vox.md").write_text('---\nprovider: "polly"\n---\n')
        result = await model(_ctx(tmp_path), "anything")
        assert result.error is True
        assert result.exit_code == 1
        assert "polly" in result.text
