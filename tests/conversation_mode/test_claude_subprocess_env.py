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


def test_keep_api_key_forwards_it_instead_of_stripping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--bare``'s opposite auth requirement -- the one call site
    that needs the key present passes keep_api_key=True."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-a-real-key")
    env = claude_subprocess_env(keep_api_key=True)
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-a-real-key"


def test_keep_api_key_default_still_strips(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default must not change for every other claude-spawn call site."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stale")
    env = claude_subprocess_env()
    assert "ANTHROPIC_API_KEY" not in env


def test_keep_api_key_does_not_forward_unrelated_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bare relay subprocess is reachable by a live, untrusted voice
    turn -- it must get a minimal environment, not the parent's secrets."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-a-real-key")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-eleven-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret")
    monkeypatch.setenv("SOME_OTHER_SECRET", "do-not-forward-me")
    env = claude_subprocess_env(keep_api_key=True)
    assert "ELEVENLABS_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert "SOME_OTHER_SECRET" not in env
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-a-real-key"


def test_keep_api_key_still_forwards_path_and_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-a-real-key")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/relay")
    env = claude_subprocess_env(keep_api_key=True)
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/relay"


def test_default_path_still_forwards_full_parent_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The non-bare call sites' existing behavior must not change --
    they forward the parent environment, minus the API key."""
    monkeypatch.setenv("SOME_OTHER_VAR", "keep-me")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stale")
    env = claude_subprocess_env()
    assert env["SOME_OTHER_VAR"] == "keep-me"
    assert "ANTHROPIC_API_KEY" not in env
