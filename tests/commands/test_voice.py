"""Tests for :obj:`punt_vox.commands.voice` -- the pure command function.

Exercised directly through a real :class:`~punt_vox.config.ConfigStore` on
a tmp dir and a stub voxd client; no CLI runner, no MCP round-trip.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.commands import Ctx, voice
from punt_vox.config import ConfigStore


def _ctx(tmp_path: Path, *, voices: list[str] | None = None) -> Ctx:
    """Build a Ctx with a real ConfigStore and a stub client returning *voices*.

    Seeds ``vox.md`` with ``provider: elevenlabs`` so listing has an
    authoritative provider to fetch a roster for -- an unset provider is
    now the F1 refusal (state is the sole authority on which provider
    voxd runs). Tests that mean to exercise the refusal overwrite the
    seeded file explicitly.
    """
    vox_md = tmp_path / "vox.md"
    if not vox_md.exists():
        vox_md.write_text('---\nprovider: "elevenlabs"\n---\n')
    client = MagicMock()
    client.voices.return_value = voices if voices is not None else []
    return Ctx(store=ConfigStore(tmp_path), client=client)


class TestList:
    """No name given -- reach voxd for the roster."""

    async def test_lists_roster(self, tmp_path: Path) -> None:
        result = await voice(_ctx(tmp_path, voices=["matilda", "roger"]), None)
        assert result.error is False
        assert "matilda" in result.text
        assert "roger" in result.text
        assert result.json_data is not None
        names = cast("list[str]", result.json_data["names"])
        assert names == ["matilda", "roger"]

    async def test_marks_current(self, tmp_path: Path) -> None:
        (tmp_path / "vox.md").write_text(
            '---\nprovider: "elevenlabs"\nvoice: "roger"\n---\n'
        )
        result = await voice(_ctx(tmp_path, voices=["matilda", "roger"]), None)
        assert "roger (current)" in result.text
        assert result.json_data is not None
        assert result.json_data["current"] == "roger"

    async def test_passes_provider_through(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path, voices=["joanna"])
        result = await voice(ctx, None, provider="polly")
        assert result.error is False
        # The stub client received the caller-supplied provider unchanged.
        ctx.client.voices.assert_called_once_with("polly")  # type: ignore[attr-defined]
        assert result.json_data is not None
        assert result.json_data.get("provider") == "polly"

    async def test_daemon_connection_error_returns_error(self, tmp_path: Path) -> None:
        (tmp_path / "vox.md").write_text('---\nprovider: "elevenlabs"\n---\n')
        ctx = Ctx(store=ConfigStore(tmp_path), client=MagicMock())
        ctx.client.voices.side_effect = VoxdConnectionError("not running")  # type: ignore[attr-defined]
        result = await voice(ctx, None)
        assert result.error is True
        assert result.exit_code == 1
        assert "not running" in result.text
        assert result.json_data is not None
        assert result.json_data["error"] == "not running"

    async def test_daemon_protocol_error_returns_error(self, tmp_path: Path) -> None:
        (tmp_path / "vox.md").write_text('---\nprovider: "elevenlabs"\n---\n')
        ctx = Ctx(store=ConfigStore(tmp_path), client=MagicMock())
        ctx.client.voices.side_effect = VoxdProtocolError("bad wire")  # type: ignore[attr-defined]
        result = await voice(ctx, None)
        assert result.error is True
        assert result.exit_code == 1

    async def test_unconfigured_provider_lists_empty_no_refusal(
        self, tmp_path: Path
    ) -> None:
        """``vox voices`` with no provider lists an empty roster, does not refuse.

        Listing is how a user discovers what to configure; refusing to list
        because nothing is configured yet would be the worst possible moment
        to say "configure something first". Symmetric with ``vox model``'s
        list branch. The set path (``vox voice <name>``) still refuses --
        writing a wrong-provider voice into ``vox.md`` is the substitution
        this bead exists to prevent.
        """
        client = MagicMock()
        result = await voice(Ctx(store=ConfigStore(tmp_path), client=client), None)
        assert result.error is False
        assert result.exit_code == 0
        assert result.text == "No voices for this provider."
        assert result.json_data is not None
        assert cast("list[str]", result.json_data["names"]) == []
        assert result.json_data["provider"] is None
        # Empty branch must not call the daemon: VoxClientSync.voices
        # requires a provider now.
        client.voices.assert_not_called()


class TestSet:
    """A name given -- normalize and write to config."""

    async def test_writes_normalized_voice(self, tmp_path: Path) -> None:
        result = await voice(_ctx(tmp_path), "matilda")
        assert result.error is False
        assert result.json_data == {"voice": "matilda"}
        assert "matilda" in result.text
        assert ConfigStore(tmp_path).read().voice == "matilda"

    async def test_strips_leading_at_sigil(self, tmp_path: Path) -> None:
        # SynthesisSpec.normalize_voice strips a stray ``@`` so the CLI and MCP
        # both accept "@matilda" as "matilda". The stored value is normalized.
        result = await voice(_ctx(tmp_path), "@matilda")
        assert result.error is False
        assert ConfigStore(tmp_path).read().voice == "matilda"

    async def test_blank_name_returns_error(self, tmp_path: Path) -> None:
        result = await voice(_ctx(tmp_path), "@")
        assert result.error is True
        assert result.exit_code == 1
        assert "empty" in result.text

    async def test_unconfigured_provider_refuses_set(self, tmp_path: Path) -> None:
        """Setting a voice with no provider configured refuses, never writes.

        A voice name is provider-scoped, so writing ``matilda`` into
        ``vox.md`` while no provider is set would land a wrong-provider
        voice the moment a caller runs ``vox provider <name>``. Symmetric
        with ``vox model``'s set-refusal; unlike the list branch, setting
        needs an authoritative provider.
        """
        # Overwrite the seeded vox.md to remove the provider entry.
        (tmp_path / "vox.md").write_text("---\n---\n")
        client = MagicMock()
        result = await voice(Ctx(store=ConfigStore(tmp_path), client=client), "matilda")
        assert result.error is True
        assert result.exit_code == 1
        assert "no TTS provider" in result.text
        assert ConfigStore(tmp_path).read_field("voice") is None
        client.voices.assert_not_called()

    async def test_config_write_error_returns_envelope(self, tmp_path: Path) -> None:
        """ConfigStore rejects control chars -- the command envelopes it, not raises.

        Historical footgun: a voice string with a newline or unescaped quote
        raised ConfigValueError across the CommandResult boundary. The command
        must catch it and return an error result.
        """
        result = await voice(_ctx(tmp_path), 'matilda"; injected: "bad')
        assert result.error is True
        assert result.exit_code == 1
        assert "Error" in result.text or "quote" in result.text.lower()
