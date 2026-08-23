"""Tests for :class:`ClaudeSessionAttach`, mocking the ``claude`` subprocess.

Per the ADR's option D: one ``claude -p --resume`` subprocess per turn,
writing one JSON user-message to stdin and reading a stream-json reply from
stdout. These tests never spawn a real ``claude`` process -- the live
concurrent-resume-safety spike the ADR calls for was run separately.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from punt_vox.voxd.conversation_mode import claude_session_attach
from punt_vox.voxd.conversation_mode.claude_session_attach import ClaudeSessionAttach
from punt_vox.voxd.conversation_mode.session_attach import (
    BareAuthMissingError,
    SessionAttachError,
)
from punt_vox.voxd.conversation_mode.turn import TranscribedTurn


@pytest.fixture(autouse=True)
def _bare_auth(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """``--bare`` requires ``ANTHROPIC_API_KEY`` -- every test but the one
    exercising its absence needs a key present, or every ``_spawn`` in this
    file fails immediately with :class:`BareAuthMissingError` before
    reaching the behavior under test."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")


def _discard(_written: bytes) -> None:
    """A stdin sink that discards whatever :meth:`ClaudeSessionAttach` writes."""


def _fake_process(stdout_lines: list[bytes], returncode: int = 0) -> AsyncMock:
    process = AsyncMock()
    process.stdin = AsyncMock()
    process.stdin.write = _discard
    process.stdin.close = MagicMock()
    process.stdout = _AsyncLineIterator(stdout_lines)
    process.stderr = AsyncMock()
    process.stderr.read = AsyncMock(return_value=b"")
    process.wait = AsyncMock(return_value=returncode)
    process.returncode = returncode
    return process


class _AsyncLineIterator:
    """A minimal async iterator standing in for ``asyncio.StreamReader``."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    def __aiter__(self) -> _AsyncLineIterator:
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


async def test_send_turn_collects_text_deltas_into_one_final_chunk() -> None:
    def _frame(text: str) -> bytes:
        payload = json.dumps(
            {"type": "assistant", "message": {"content": [{"text": text}]}}
        )
        return payload.encode() + b"\n"

    lines = [_frame("Hi"), _frame(" there"), b'{"type":"result"}\n']
    process = _fake_process(lines)
    with patch("asyncio.create_subprocess_exec", return_value=process):
        attach = ClaudeSessionAttach(session_id="session-a")
        chunks = [
            chunk async for chunk in attach.send_turn(TranscribedTurn(text="hello"))
        ]
    (chunk,) = chunks
    assert chunk.text == "Hi there"
    assert chunk.is_final


async def test_nonzero_exit_raises_session_attach_error() -> None:
    process = _fake_process([], returncode=1)
    process.stderr.read = AsyncMock(return_value=b"agent session ended abnormally")
    with patch("asyncio.create_subprocess_exec", return_value=process):
        attach = ClaudeSessionAttach(session_id="session-a")
        with pytest.raises(SessionAttachError, match="agent session ended abnormally"):
            async for _ in attach.send_turn(TranscribedTurn(text="hello")):
                pass


async def test_malformed_stream_json_line_raises_session_attach_error() -> None:
    process = _fake_process([b"not json\n"])
    process.kill = MagicMock()
    with patch("asyncio.create_subprocess_exec", return_value=process):
        attach = ClaudeSessionAttach(session_id="session-a")
        with pytest.raises(SessionAttachError, match="malformed stream-json"):
            async for _ in attach.send_turn(TranscribedTurn(text="hello")):
                pass
    # Regression: a non-timeout failure mid-exchange must still kill and
    # reap the subprocess -- without this, every malformed-frame occurrence
    # leaked a `claude -p --resume` zombie.
    process.kill.assert_called_once()
    process.wait.assert_awaited()


async def test_spawn_argv_includes_verbose() -> None:
    """Regression: claude now refuses to start at all -- exit 1 before a
    single reply frame -- when -p/--output-format=stream-json is combined
    without --verbose ("requires --verbose").
    """
    process = _fake_process([])
    with patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec:
        attach = ClaudeSessionAttach(session_id="session-a")
        async for _ in attach.send_turn(TranscribedTurn(text="hello")):
            pass
    argv = mock_exec.call_args.args
    assert "--verbose" in argv


async def test_spawn_keeps_anthropic_api_key_in_the_subprocess_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """vox-36xc: --bare has no OAuth support at all -- the opposite of the
    prior non-bare behavior, where a stale ANTHROPIC_API_KEY was stripped
    so the resumed session used its own claude.ai login instead. Bare mode
    *requires* the key explicitly (`claude --help`: "Anthropic auth is
    strictly ANTHROPIC_API_KEY or apiKeyHelper via --settings"), so this
    call site must forward it, not strip it.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-a-real-key")
    process = _fake_process([])
    with patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec:
        attach = ClaudeSessionAttach(session_id="session-a")
        async for _ in attach.send_turn(TranscribedTurn(text="hello")):
            pass
    spawned_env = mock_exec.call_args.kwargs["env"]
    assert spawned_env["ANTHROPIC_API_KEY"] == "sk-ant-a-real-key"
    # The relay marker must still be present alongside it.
    assert spawned_env["VOX_CALL_RELAY"] == "1"


async def test_spawn_argv_includes_bare() -> None:
    """--bare eliminates the SessionStart hook cascade (0 hooks vs 9-28
    measured), most of the 13-25s median per-turn spawn latency."""
    process = _fake_process([])
    with patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec:
        attach = ClaudeSessionAttach(session_id="session-a")
        async for _ in attach.send_turn(TranscribedTurn(text="hello")):
            pass
    argv = mock_exec.call_args.args
    assert "--bare" in argv


async def test_missing_api_key_raises_before_spawning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--bare has no OAuth fallback -- an absent key is a certain failure,
    so it must be caught before the subprocess is even spawned, not left to
    run into the 120s reply timeout and surface as an opaque "did not reply
    within 120s".
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        attach = ClaudeSessionAttach(session_id="session-a")
        with pytest.raises(BareAuthMissingError, match="ANTHROPIC_API_KEY"):
            async for _ in attach.send_turn(TranscribedTurn(text="hello")):
                pass
    mock_exec.assert_not_called()


async def test_spawn_raises_the_stdout_line_limit() -> None:
    """Regression: the default 64KB StreamReader line-buffer limit raised
    ValueError ("Separator is not found, and chunk exceed the limit") on
    readline() once --verbose's larger per-line payloads exceeded it,
    before any real reply content was ever collected.
    """
    process = _fake_process([])
    with patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec:
        attach = ClaudeSessionAttach(session_id="session-a")
        async for _ in attach.send_turn(TranscribedTurn(text="hello")):
            pass
    expected_limit = claude_session_attach._STDOUT_LINE_LIMIT
    assert mock_exec.call_args.kwargs["limit"] == expected_limit


async def test_collect_reply_handles_a_line_larger_than_the_old_64kb_default() -> None:
    """The real regression, exercised against a real ``asyncio.StreamReader``
    (not the mocked process the other tests use) configured with the
    production limit: a single stream-json line comfortably past the old
    64KB default must still be read and collected, not raise.
    """
    huge_text = "x" * (100 * 1024)  # 100KB -- well past the old 64KB default
    frame = json.dumps(
        {"type": "assistant", "message": {"content": [{"text": huge_text}]}}
    )
    reader = asyncio.StreamReader(limit=claude_session_attach._STDOUT_LINE_LIMIT)
    reader.feed_data(frame.encode() + b"\n")
    reader.feed_eof()

    attach = ClaudeSessionAttach(session_id="session-a")
    text = await attach._collect_reply(reader)
    assert text == huge_text


async def test_verbose_system_frames_are_not_treated_as_reply_text() -> None:
    """Regression: --verbose adds type="system" frames (hook lifecycle,
    session init) alongside the existing assistant/result frames -- none
    of them carry a top-level "message" field shaped like an assistant
    delta, so they must not leak into the collected reply text.
    """

    def _system_frame(subtype: str) -> bytes:
        payload = json.dumps(
            {"type": "system", "subtype": subtype, "session_id": "session-a"}
        )
        return payload.encode() + b"\n"

    def _assistant_frame(text: str) -> bytes:
        payload = json.dumps(
            {"type": "assistant", "message": {"content": [{"text": text}]}}
        )
        return payload.encode() + b"\n"

    lines = [
        _system_frame("hook_started"),
        _system_frame("hook_response"),
        _system_frame("init"),
        _assistant_frame("The answer is 4."),
        b'{"type":"result"}\n',
    ]
    process = _fake_process(lines)
    with patch("asyncio.create_subprocess_exec", return_value=process):
        attach = ClaudeSessionAttach(session_id="session-a")
        chunks = [
            chunk async for chunk in attach.send_turn(TranscribedTurn(text="hello"))
        ]
    (chunk,) = chunks
    assert chunk.text == "The answer is 4."


async def test_writes_the_turn_as_a_user_message_to_stdin() -> None:
    process = _fake_process([])
    written: list[bytes] = []
    process.stdin.write = written.append
    with patch("asyncio.create_subprocess_exec", return_value=process):
        attach = ClaudeSessionAttach(session_id="session-a")
        async for _ in attach.send_turn(TranscribedTurn(text="turn on the lights")):
            pass
    (payload,) = written
    assert b'"role": "user"' in payload
    assert b"turn on the lights" in payload


class _HangingLineIterator:
    """Never yields a line -- simulates a stalled subprocess for the timeout test."""

    def __aiter__(self) -> _HangingLineIterator:
        return self

    async def __anext__(self) -> bytes:
        await asyncio.sleep(10)
        raise StopAsyncIteration  # pragma: no cover -- the timeout fires first


async def test_stalled_reply_times_out_and_kills_the_process() -> None:
    process = _fake_process([])
    process.stdout = _HangingLineIterator()
    process.kill = MagicMock()
    with (
        patch("asyncio.create_subprocess_exec", return_value=process),
        patch(
            "punt_vox.voxd.conversation_mode.claude_session_attach._REPLY_TIMEOUT_S",
            0.05,
        ),
    ):
        attach = ClaudeSessionAttach(session_id="session-a")
        with pytest.raises(SessionAttachError, match="did not reply within"):
            async for _ in attach.send_turn(TranscribedTurn(text="hello")):
                pass
    process.kill.assert_called_once()
    process.wait.assert_awaited()


async def test_stderr_is_drained_alongside_stdout() -> None:
    """Regression: stdout-then-stderr sequencing risks a pipe deadlock."""
    process = _fake_process([])
    process.stderr.read = AsyncMock(return_value=b"some diagnostic noise")
    with patch("asyncio.create_subprocess_exec", return_value=process):
        attach = ClaudeSessionAttach(session_id="session-a")
        async for _ in attach.send_turn(TranscribedTurn(text="hello")):
            pass
    process.stderr.read.assert_awaited_once()


class _CancellingLineIterator:
    """Raises CancelledError on first read -- simulates Ctrl-C during a call."""

    def __aiter__(self) -> _CancellingLineIterator:
        return self

    async def __anext__(self) -> bytes:
        raise asyncio.CancelledError


async def test_cancellation_during_exchange_kills_and_reaps_the_process() -> None:
    """Regression: CancelledError is a BaseException, not an Exception --
    the obvious way a human ends a call (Ctrl-C) must not leak the
    subprocess just because it isn't caught by ``except Exception``."""
    process = _fake_process([])
    process.stdout = _CancellingLineIterator()
    process.kill = MagicMock()
    with patch("asyncio.create_subprocess_exec", return_value=process):
        attach = ClaudeSessionAttach(session_id="session-a")
        with pytest.raises(asyncio.CancelledError):
            async for _ in attach.send_turn(TranscribedTurn(text="hello")):
                pass
    process.kill.assert_called_once()
    process.wait.assert_awaited()


async def test_stdin_write_failure_kills_and_reaps_the_process() -> None:
    """Regression: claude can exit immediately on an invalid session id,
    breaking the pipe before this class ever reaches the read side."""
    process = _fake_process([])
    process.stdin.write = MagicMock(side_effect=BrokenPipeError)
    process.kill = MagicMock()
    with patch("asyncio.create_subprocess_exec", return_value=process):
        attach = ClaudeSessionAttach(session_id="session-a")
        with pytest.raises(BrokenPipeError):
            async for _ in attach.send_turn(TranscribedTurn(text="hello")):
                pass
    process.kill.assert_called_once()
    process.wait.assert_awaited()
