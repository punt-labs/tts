"""Tests for :mod:`punt_vox.commands.call_spec`.

Exercises :data:`resolve_call_spec` against a real
:class:`~punt_vox.config.ConfigStore` on a tmp dir (mirroring
``tests/commands/test_model.py``'s pattern) -- no CLI runner needed, since
the command object is a plain callable.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from punt_vox.commands.call_spec import resolve_call_spec


def test_resolves_provider_from_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "vox.md").write_text('---\nprovider: "elevenlabs"\n---\n')
    monkeypatch.setattr("punt_vox.session_spec.find_config_dir", lambda: tmp_path)
    spec = resolve_call_spec()
    assert spec.provider == "elevenlabs"


def test_no_provider_configured_raises_a_cli_appropriate_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: SessionSpec's own message names mic:provider, the MCP
    tool -- a CLI user cannot run it. call_spec.py must rewrite it.
    """
    (tmp_path / "vox.md").write_text("---\n---\n")
    monkeypatch.setattr("punt_vox.session_spec.find_config_dir", lambda: tmp_path)
    with pytest.raises(typer.BadParameter) as exc_info:
        resolve_call_spec()
    message = str(exc_info.value)
    assert "vox provider <name>" in message
    assert "mic:provider" not in message


def test_hand_edited_model_pair_raises_the_shared_message_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ModelNotAvailableError's message carries no MCP-flavored text, so it
    passes through unchanged rather than being rewritten.
    """
    (tmp_path / "vox.md").write_text(
        '---\nprovider: "openai"\nmodel: "not-a-real-model"\n---\n'
    )
    monkeypatch.setattr("punt_vox.session_spec.find_config_dir", lambda: tmp_path)
    with pytest.raises(typer.BadParameter) as exc_info:
        resolve_call_spec()
    message = str(exc_info.value)
    assert "not-a-real-model" in message
    assert "openai" in message
