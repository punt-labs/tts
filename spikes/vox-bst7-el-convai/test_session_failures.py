"""Offline pins for the session's failure boundaries.

The trace is the spike's evidence; these tests pin that local faults
land in it loudly instead of dying as unretrieved task exceptions: a
recv-loop crash sets the closed reason and a trace note, a tool raising
an arbitrary exception still posts an is_error result, and a dispatch
task that dies after execution (socket gone under ``_send``) leaves a
``tool_task_crashed`` note.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import WebSocketException

from convai import ConvAISession, EventTrace
from spike_tools import ToolBelt

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from websockets.asyncio.server import ServerConnection

    type ServerScript = Callable[[ServerConnection], Awaitable[None]]

# The init body deliberately carries a credential-shaped field: every
# test's trace doubles as a redaction check, and one test pins it.
_INIT_EVENT: dict[str, object] = {
    "type": "conversation_initiation_metadata",
    "conversation_initiation_metadata_event": {
        "conversation_id": "conv-fail",
        "persistent_session_token": "tok-LEAKME",
    },
}


def _tool_call(name: str, call_id: str, params: dict[str, object]) -> str:
    return json.dumps(
        {
            "type": "client_tool_call",
            "client_tool_call": {
                "tool_name": name,
                "tool_call_id": call_id,
                "parameters": params,
            },
        }
    )


def _trace_types(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [str(json.loads(line)["type"]) for line in lines if line]


async def _wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            msg = "session did not reach the expected state within timeout"
            raise TimeoutError(msg)
        await asyncio.sleep(0.01)


async def _open_session(
    tmp_path: Path, script: ServerScript, notes_path: Path | None = None
) -> tuple[ConvAISession, Path]:
    # notes_path None means the default writable location; tests that
    # exercise tool I/O failure pass an unwritable path instead.
    trace_path = tmp_path / "trace.jsonl"

    async def handler(ws: ServerConnection) -> None:
        await ws.recv()  # conversation_initiation_client_data
        await ws.send(json.dumps(_INIT_EVENT))
        await script(ws)

    server = await serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    session = ConvAISession(
        url=f"ws://127.0.0.1:{port}",
        toolbelt=ToolBelt(notes_path or tmp_path / "notes.md"),
        trace=EventTrace(trace_path),
        overrides={},
    )
    await session.open(timeout_s=5.0)
    # The server object stays alive via the returned session's lifetime;
    # each test closes the session, after which the server is dropped.
    session_server = (session, trace_path)
    # close() defaults to close_connections=True, which would race the
    # scripted conversation; stop accepting only, keep the live socket.
    server.close(close_connections=False)
    return session_server


class TestRecvLoopCrash:
    """A malformed frame must not kill the recv task silently."""

    async def test_malformed_frame_sets_closed_reason(self, tmp_path: Path) -> None:
        async def script(ws: ServerConnection) -> None:
            await ws.send("{this is not json")
            await asyncio.sleep(5.0)  # keep the socket open; the crash is local

        session, trace_path = await _open_session(tmp_path, script)
        with pytest.raises(RuntimeError, match="recv loop crashed"):
            await session.say("hello", timeout_s=5.0)
        await session.close()
        assert "recv_loop_crashed" in _trace_types(trace_path)


class TestToolExceptionBoundary:
    """Any tool exception comes back as an is_error result, not a dead task."""

    async def test_oserror_in_tool_posts_error_result(self, tmp_path: Path) -> None:
        received: list[dict[str, object]] = []

        async def script(ws: ServerConnection) -> None:
            await ws.send(_tool_call("write_note", "io-1", {"text": "boom"}))
            received.append(dict(json.loads(await ws.recv())))

        # A notes path inside a directory that does not exist: the tool's
        # file open raises FileNotFoundError (an OSError), which the old
        # (KeyError, ValueError) net let escape as a dead task.
        session, trace_path = await _open_session(
            tmp_path, script, notes_path=tmp_path / "no_such_dir" / "notes.md"
        )
        await _wait_until(lambda: bool(received))
        await session.close()

        result = received[0]
        assert result["is_error"] is True
        assert "FileNotFoundError" in str(result["result"])
        inv = session.metrics.invocations[0]
        assert inv.is_error is True
        assert inv.t_result is not None  # the invocation completed, not stuck
        assert "client_tool_result" in _trace_types(trace_path)


class TestTraceRedaction:
    """Credential-shaped fields never reach the committed trace verbatim."""

    def test_token_shaped_fields_are_masked(self) -> None:
        masked = "[redacted]"
        event: dict[str, object] = {
            "conversation_id": "conv-1",
            "persistent_session_token": "tok-abc123",
            "signed_url": "wss://api.example/ws?tok=abc",
            "client_api_key": "sk-xyz",
        }
        redacted = ConvAISession._redacted(event)
        assert redacted["conversation_id"] == "conv-1"
        assert [redacted[key] for key in event if key != "conversation_id"] == [
            masked,
            masked,
            masked,
        ]

    def test_absent_token_stays_none(self) -> None:
        # "the server sent no token" is evidence; a masked None would
        # erase the distinction between absent and present-but-hidden.
        redacted = ConvAISession._redacted({"persistent_session_token": None})
        assert redacted["persistent_session_token"] is None

    async def test_init_metadata_trace_line_is_redacted(self, tmp_path: Path) -> None:
        # _INIT_EVENT carries a token below; the recorded line must not.
        async def script(_ws: ServerConnection) -> None:
            await asyncio.sleep(1.0)

        session, trace_path = await _open_session(tmp_path, script)
        await session.close()
        raw = trace_path.read_text(encoding="utf-8")
        assert "tok-LEAKME" not in raw
        assert "[redacted]" in raw

    async def test_connect_error_names_host_only(self, tmp_path: Path) -> None:
        session = ConvAISession(
            url="wss://api.example.io/v1/convai?tok=SECRET-BEARER",
            toolbelt=ToolBelt(tmp_path / "notes.md"),
            trace=EventTrace(tmp_path / "trace.jsonl"),
            overrides={},
        )
        error = session._connect_error(WebSocketException("boom SECRET-BEARER"))
        # Exact match: the sanitized message is host + exception type and
        # nothing else -- no token, no query string, no library text.
        assert str(error) == (
            "websocket connect to api.example.io rejected: WebSocketException"
        )


class TestTurnCompletion:
    """A pending invocation holds say() open until the post-tool answer."""

    async def test_say_waits_for_the_post_tool_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Shrink the clocks, keep the shape: tool execution (1.0s) must
        # exceed the turn grace (0.4s), the exact geometry where the old
        # predicate declared the turn over before the post-tool answer.
        monkeypatch.setattr("convai._TURN_GRACE_S", 0.4)
        monkeypatch.setattr("spike_tools._SLOW_SCHEDULE_S", (1.0,))

        async def script(ws: ServerConnection) -> None:
            await ws.recv()  # the user_message from say()
            await ws.send(
                json.dumps(
                    {
                        "type": "agent_response",
                        "agent_response_event": {"agent_response": "Let me search."},
                    }
                )
            )
            await ws.send(_tool_call("search_code", "turn-1", {"pattern": "x"}))
            await ws.recv()  # client_tool_result, ~1.0s later
            await asyncio.sleep(0.2)
            await ws.send(
                json.dumps(
                    {
                        "type": "agent_response",
                        "agent_response_event": {
                            "agent_response": "I found 3 matches."
                        },
                    }
                )
            )
            # Keep the socket open past the turn grace; the handler
            # returning would close it mid-turn and fail say() early.
            await asyncio.sleep(3.0)

        session, _trace_path = await _open_session(tmp_path, script)
        reply = await session.say("search please", timeout_s=10.0)
        await session.close()
        assert "I found 3 matches." in reply


class TestToolTaskCrashNote:
    """A dispatch task dying after execution lands in the trace."""

    async def test_send_failure_records_tool_task_crashed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Slow the tool so the server's close lands while it executes;
        # the post-execution _send then hits a closed socket.
        monkeypatch.setattr("spike_tools._SLOW_SCHEDULE_S", (0.2,))

        async def script(ws: ServerConnection) -> None:
            await ws.send(_tool_call("search_code", "dead-1", {"pattern": "x"}))
            await ws.close()

        session, trace_path = await _open_session(tmp_path, script)
        await _wait_until(lambda: "tool_task_crashed" in _trace_types(trace_path))
        await session.close()
        assert "ws_closed" in _trace_types(trace_path)
