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


def _ctx(tmp_path: Path, voices: list[str] | None = None) -> Ctx:
    """Build a Ctx with a real ConfigStore and a voxd client stub for cascade.

    Seeds ``vox.md`` with ``provider: elevenlabs`` so the command has an
    authoritative provider to resolve models against -- an unset provider
    is the F1 refusal now (see :func:`~punt_vox.session_spec.SessionSpec`),
    and every test that predates that rule assumed the "default to
    elevenlabs" substitution this bead deletes.

    The cascade rule fetches the current provider's voice roster on every
    model set; tests supply what that roster returns via *voices*.
    """
    vox_md = tmp_path / "vox.md"
    if not vox_md.exists():
        vox_md.write_text('---\nprovider: "elevenlabs"\n---\n')
    client = MagicMock()
    client.voices.return_value = voices if voices is not None else []
    return Ctx(store=ConfigStore(tmp_path), client=client)


class TestList:
    """No name given -- list the current provider's models."""

    async def test_configured_provider_lists_its_models(self, tmp_path: Path) -> None:
        """The provider is read from state -- no ``or "elevenlabs"`` substitution."""
        result = await model(_ctx(tmp_path), None)
        assert result.error is False
        assert "eleven_v3" in result.text
        assert result.json_data is not None
        names = cast("list[str]", result.json_data["names"])
        assert "eleven_v3" in names

    async def test_unconfigured_provider_lists_empty_no_substitution(
        self, tmp_path: Path
    ) -> None:
        """No provider configured -- the list is empty, not ElevenLabs's roster.

        Previously the command substituted ``"elevenlabs"`` for an unset
        provider and reported its models as the session's current list;
        that substitution is exactly what this bead deletes. Listing gets
        an honest empty answer instead.
        """
        # Overwrite the seeded vox.md to remove the provider entry.
        (tmp_path / "vox.md").write_text("---\n---\n")
        client = MagicMock()
        client.voices.return_value = []
        result = await model(Ctx(store=ConfigStore(tmp_path), client=client), None)
        assert result.error is False
        assert result.json_data is not None
        assert cast("list[str]", result.json_data["names"]) == []
        assert result.text == "No models for this provider."

    async def test_unconfigured_provider_rejects_a_set(self, tmp_path: Path) -> None:
        """Setting a model with no provider configured refuses, never substitutes."""
        (tmp_path / "vox.md").write_text("---\n---\n")
        client = MagicMock()
        client.voices.return_value = []
        result = await model(Ctx(store=ConfigStore(tmp_path), client=client), "v3")
        assert result.error is True
        assert result.exit_code == 1
        assert "no TTS provider" in result.text

    async def test_marks_current(self, tmp_path: Path) -> None:
        (tmp_path / "vox.md").write_text(
            '---\nprovider: "elevenlabs"\nmodel: "eleven_flash_v2_5"\n---\n'
        )
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

    async def test_shorthand_resolves_and_cascades_voice(self, tmp_path: Path) -> None:
        """The cascade rule (vox-awm9): setting model populates voice = first."""
        result = await model(_ctx(tmp_path, voices=["matilda", "roger"]), "v3")
        assert result.error is False
        assert result.json_data == {"model": "eleven_v3", "voice": "matilda"}
        assert result.text == "Model: eleven_v3"
        # Full round-trip: reading through ConfigStore sees both writes.
        cfg = ConfigStore(tmp_path).read()
        assert cfg.model == "eleven_v3"
        assert cfg.voice == "matilda"

    async def test_full_name_is_accepted(self, tmp_path: Path) -> None:
        result = await model(_ctx(tmp_path, voices=["matilda"]), "eleven_turbo_v2_5")
        assert result.error is False
        assert result.json_data == {
            "model": "eleven_turbo_v2_5",
            "voice": "matilda",
        }

    async def test_cascade_with_empty_roster_writes_blank_voice(
        self, tmp_path: Path
    ) -> None:
        """A daemon that returns no voices cascades voice = '' (cleared on disk)."""
        result = await model(_ctx(tmp_path, voices=[]), "v3")
        assert result.error is False
        assert result.json_data == {"model": "eleven_v3", "voice": ""}
        cfg = ConfigStore(tmp_path).read()
        assert cfg.voice in (None, "")

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

    async def test_roster_fetch_error_aborts_the_write(self, tmp_path: Path) -> None:
        """A daemon fault on the cascade roster fetch returns an error envelope."""
        from punt_vox.client_errors import VoxdConnectionError

        client = MagicMock()
        client.voices.side_effect = VoxdConnectionError("voxd unreachable")
        ctx = Ctx(store=ConfigStore(tmp_path), client=client)

        result = await model(ctx, "v3")

        assert result.error is True
        assert result.exit_code == 1
        # The model must not have landed on disk.
        cfg = ConfigStore(tmp_path).read()
        assert cfg.model is None
