"""Tests for :class:`ClaudeSessionAttach`, mocking the ``claude`` subprocess.

Per the ADR's option D: one ``claude -p --resume`` subprocess per turn,
writing one JSON user-message to stdin and reading a stream-json reply from
stdout. These tests never spawn a real ``claude`` process -- the live
concurrent-resume-safety spike the ADR calls for was run separately, before
this mission's operator ratification (per this mission's contract context).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from punt_vox.voxd.conversation_mode.claude_session_attach import ClaudeSessionAttach
from punt_vox.voxd.conversation_mode.session_attach import SessionAttachError
from punt_vox.voxd.conversation_mode.turn import TranscribedTurn


def _discard(_written: bytes) -> None:
    """A stdin sink that discards whatever :meth:`ClaudeSessionAttach` writes."""


def _fake_process(stdout_lines: list[bytes], returncode: int = 0) -> AsyncMock:
    process = AsyncMock()
    process.stdin = AsyncMock()
    process.stdin.write = _discard
    process.stdin.close = MagicMock()
    process.stdout = _AsyncLineIterator(stdout_lines)
    process.communicate.return_value = (b"", b"")
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
    process.communicate.return_value = (b"", b"agent session ended abnormally")
    with patch("asyncio.create_subprocess_exec", return_value=process):
        attach = ClaudeSessionAttach(session_id="session-a")
        with pytest.raises(SessionAttachError, match="agent session ended abnormally"):
            async for _ in attach.send_turn(TranscribedTurn(text="hello")):
                pass


async def test_malformed_stream_json_line_raises_session_attach_error() -> None:
    process = _fake_process([b"not json\n"])
    with patch("asyncio.create_subprocess_exec", return_value=process):
        attach = ClaudeSessionAttach(session_id="session-a")
        with pytest.raises(SessionAttachError, match="malformed stream-json"):
            async for _ in attach.send_turn(TranscribedTurn(text="hello")):
                pass


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
