"""Tests for :func:`claude_subprocess_env`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from punt_vox.voxd.conversation_mode.claude_subprocess_env import claude_subprocess_env

if TYPE_CHECKING:
    import pytest


def test_strips_anthropic_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stale")
    env = claude_subprocess_env()
    assert "ANTHROPIC_API_KEY" not in env


def test_preserves_other_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_OTHER_VAR", "keep-me")
    env = claude_subprocess_env()
    assert env["SOME_OTHER_VAR"] == "keep-me"


def test_extra_is_merged_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VOX_CALL_RELAY", raising=False)
    env = claude_subprocess_env(extra={"VOX_CALL_RELAY": "1"})
    assert env["VOX_CALL_RELAY"] == "1"


def test_extra_wins_over_a_same_named_parent_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOME_VAR", "from-parent")
    env = claude_subprocess_env(extra={"SOME_VAR": "from-extra"})
    assert env["SOME_VAR"] == "from-extra"


def test_no_extra_still_returns_a_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stale")
    env = claude_subprocess_env()
    assert isinstance(env, dict)
    assert "ANTHROPIC_API_KEY" not in env
