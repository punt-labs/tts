# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx>=0.27", "websockets>=14"]
# ///
"""Offline dry run: drive the harness against a local mock EL server.

No API key, no credits, no network. Verifies the full client pipeline --
initiation, tool dispatch, result posting, round-trip clocks, turn
completion, metrics aggregation -- before the first real (billed) run.

    uv run dry_run.py
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Self, final

from websockets.asyncio.server import ServerConnection, serve

from convai import ConvAISession, EventTrace
from run_automated import MetricsReport
from spike_tools import ToolBelt

_HERE = Path(__file__).parent

# user_message keyword -> tool call the mock agent issues.
_KEYWORD_TOOLS: tuple[tuple[str, str, dict[str, object]], ...] = (
    ("time", "clock", {}),
    ("search", "search_code", {"pattern": "playback queue"}),
    ("note", "write_note", {"text": "dry run note"}),
)

_TURNS: tuple[str, ...] = (
    "What time is it?",
    "Search the code for the playback queue and write a note about it.",
    "Say goodbye.",
)


@final
class MockElServer:
    """Speaks just enough of the EL Conv AI protocol to exercise the client."""

    _calls: int

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._calls = 0
        return self

    async def handle(self, ws: ServerConnection) -> None:
        """One conversation: initiation, then tool calls per user message."""
        async for raw in ws:
            message = json.loads(raw)
            msg_type = str(message.get("type", ""))
            if msg_type == "conversation_initiation_client_data":
                await ws.send(
                    json.dumps(
                        {
                            "type": "conversation_initiation_metadata",
                            "conversation_initiation_metadata_event": {
                                "conversation_id": "dry-run-conversation",
                                "agent_output_audio_format": "pcm_16000",
                                "user_input_audio_format": "pcm_16000",
                            },
                        }
                    )
                )
            elif msg_type == "user_message":
                await self._agent_turn(ws, str(message.get("text", "")))

    async def _agent_turn(self, ws: ServerConnection, text: str) -> None:
        lowered = text.lower()
        pending: list[str] = []
        for keyword, tool, params in _KEYWORD_TOOLS:
            if keyword in lowered:
                self._calls += 1
                call_id = f"call-{self._calls}"
                pending.append(call_id)
                await ws.send(
                    json.dumps(
                        {
                            "type": "client_tool_call",
                            "client_tool_call": {
                                "tool_name": tool,
                                "tool_call_id": call_id,
                                "parameters": params,
                            },
                        }
                    )
                )
        while pending:
            reply = json.loads(await ws.recv())
            if reply.get("type") == "client_tool_result":
                pending.remove(str(reply["tool_call_id"]))
        # Simulated LLM continuation delay after the last tool result.
        await asyncio.sleep(0.35)
        await ws.send(
            json.dumps(
                {
                    "type": "agent_response",
                    "agent_response_event": {"agent_response": f"Done with: {text}"},
                }
            )
        )


async def _run() -> None:
    server = MockElServer()
    async with serve(server.handle, "127.0.0.1", 0) as running:
        port = running.sockets[0].getsockname()[1]
        trace = EventTrace(_HERE / "results" / "trace_dry_run.jsonl")
        session = ConvAISession(
            url=f"ws://127.0.0.1:{port}",
            toolbelt=ToolBelt(_HERE / "results" / "dry_run_notes.txt"),
            trace=trace,
            overrides={"conversation": {"text_only": True}},
        )
        t0 = time.monotonic()
        await session.open()
        for turn in _TURNS:
            reply = await session.say(turn, timeout_s=30.0)
            print(f"you>   {turn}")
            print(f"agent> {reply}")
        await session.close()
        elapsed = time.monotonic() - t0
    completed = session.metrics.completed_invocations
    run_record: dict[str, object] = {
        "invocations": [inv.as_dict() for inv in completed]
    }
    report = MetricsReport([run_record])
    print()
    print(report.table())
    print()
    print(f"invocations: {len(completed)} complete, elapsed {elapsed:.1f}s")
    incomplete = len(session.metrics.invocations) - len(completed)
    if incomplete:
        msg = f"{incomplete} invocations never saw a follow-up event"
        raise RuntimeError(msg)
    print("dry run OK")


def main() -> None:
    """Run the offline protocol exercise."""
    asyncio.run(_run())


if __name__ == "__main__":
    main()
