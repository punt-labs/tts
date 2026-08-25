"""Tests for :data:`reply_redactor`."""

from __future__ import annotations

from punt_vox.voxd.conversation_mode.reply_redaction import reply_redactor


def test_redacts_openai_style_key() -> None:
    text = (
        "Your key is sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789 -- keep it safe."
    )
    redacted = reply_redactor(text)
    assert "sk-ant-api03" not in redacted
    assert "[redacted]" in redacted


def test_redacts_github_token() -> None:
    text = "found ghp_abcdefghijklmnopqrstuvwxyz0123456789 in the file"
    redacted = reply_redactor(text)
    assert "ghp_" not in redacted
    assert "[redacted]" in redacted


def test_redacts_aws_access_key() -> None:
    text = "AWS_ACCESS_KEY_ID is AKIAABCDEFGHIJKLMNOP"
    redacted = reply_redactor(text)
    assert "AKIAABCDEFGHIJKLMNOP" not in redacted


def test_redacts_key_value_assignment() -> None:
    text = "the .env file has ELEVENLABS_API_KEY=abc123-super-secret-value-here"
    redacted = reply_redactor(text)
    assert "abc123-super-secret-value-here" not in redacted
    assert "[redacted]" in redacted


def test_redacts_high_entropy_run() -> None:
    text = "the token is dGhpc2lzYXZlcnlsb25nYmFzZTY0ZW5jb2RlZHNlY3JldHZhbHVl now"
    redacted = reply_redactor(text)
    assert "dGhpc2lzYXZlcnlsb25nYmFzZTY0ZW5jb2RlZHNlY3JldHZhbHVl" not in redacted


def test_passes_through_ordinary_text_unchanged() -> None:
    text = "The weather today is sunny with a high of seventy two degrees."
    assert reply_redactor(text) == text


def test_passes_through_short_technical_text_unchanged() -> None:
    text = "Run `make check` before committing, then push the branch."
    assert reply_redactor(text) == text
