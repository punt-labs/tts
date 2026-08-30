# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx>=0.27", "websockets>=14"]
# ///
"""Offline barge-in rehearsal: the audio-injection flow vs a mock EL server.

No API key, no credits, no network. The mock speaks the Conv AI audio
protocol just far enough to rehearse the barge-in script: it detects
speech in the injected PCM, emits ``interruption`` at speech onset while
a tool call is pending (as EL's VAD does), and answers the recall probe
with the tool's findings. The run must adjudicate PASS before any billed
run is allowed.

    uv run dry_run_barge_in.py
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Self, final

from websockets.asyncio.server import ServerConnection, serve

from barge_in import (
    INTERRUPT_TEXT,
    NOTE_TEXT,
    PROBE_TEXT,
    TRIGGER_TEXT,
    BargeInFlow,
    BargeInUtterances,
    SyntheticAudio,
)
from barge_in_verdict import BargeInAdjudicator, Verdict
from convai import ConvAISession, EventTrace
from speech import EspeakSynth
from spike_tools import ToolBelt

_HERE = Path(__file__).parent

_MIN_SPEECH_CHUNKS = 3  # ~192ms of sound before an utterance counts
_END_SILENCE_CHUNKS = 12  # ~768ms; espeak's longest internal pause is ~384ms


@final
class MockBargeInServer:
    """Speaks enough of the EL audio protocol to rehearse the barge-in flow.

    Speech detection is chunk-level: the synthetic mic sends pure-zero
    silence between utterances, so any nonzero chunk is speech. Utterance
    boundaries drive a scripted conversation mirroring the flow's four
    steps; an interruption fires at speech onset while a tool is pending.
    """

    _calls: int
    _pending: dict[str, str]  # tool_call_id -> tool_name
    _in_utterance: bool
    _speech_chunks: int
    _silence_run: int
    _utterance_index: int

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._calls = 0
        self._pending = {}
        self._in_utterance = False
        self._speech_chunks = 0
        self._silence_run = 0
        self._utterance_index = 0
        return self

    async def handle(self, ws: ServerConnection) -> None:
        """One conversation: initiation, then audio chunks and tool results."""
        async for raw in ws:
            message = json.loads(raw)
            if "user_audio_chunk" in message:
                pcm = base64.b64decode(str(message["user_audio_chunk"]))
                await self._on_audio_chunk(ws, pcm)
            elif message.get("type") == "conversation_initiation_client_data":
                await self._greet(ws)
            elif message.get("type") == "client_tool_result":
                await self._on_tool_result(ws, message)

    async def _greet(self, ws: ServerConnection) -> None:
        await self._send(
            ws,
            {
                "type": "conversation_initiation_metadata",
                "conversation_initiation_metadata_event": {
                    "conversation_id": "dry-run-barge-in",
                    "agent_output_audio_format": "pcm_16000",
                    "user_input_audio_format": "pcm_16000",
                },
            },
        )
        await self._agent_says(ws, "Hi! How can I help?")
        # A little agent audio so the sink's play path is exercised.
        await self._send(
            ws,
            {
                "type": "audio",
                "audio_event": {
                    "audio_base_64": base64.b64encode(bytes(3200)).decode(),
                    "event_id": 1,
                },
            },
        )

    async def _on_audio_chunk(self, ws: ServerConnection, pcm: bytes) -> None:
        speaking = any(pcm)
        if speaking:
            if not self._in_utterance:
                self._in_utterance = True
                self._speech_chunks = 0
                await self._on_speech_start(ws)
            self._speech_chunks += 1
            self._silence_run = 0
            return
        if not self._in_utterance:
            return
        self._silence_run += 1
        if self._silence_run >= _END_SILENCE_CHUNKS:
            self._in_utterance = False
            if self._speech_chunks >= _MIN_SPEECH_CHUNKS:
                self._utterance_index += 1
                await self._on_utterance_end(ws, self._utterance_index)

    async def _on_speech_start(self, ws: ServerConnection) -> None:
        # EL's VAD interrupts at speech onset; the mock barges in only
        # when a tool call is still outstanding -- the scenario under test.
        if not self._pending:
            return
        await self._send(
            ws, {"type": "interruption", "interruption_event": {"event_id": 2}}
        )
        await self._send(
            ws,
            {
                "type": "agent_response_correction",
                "agent_response_correction_event": {
                    "original_agent_response": "Searching the code now.",
                    "corrected_agent_response": "Searching the co...",
                },
            },
        )

    async def _on_utterance_end(self, ws: ServerConnection, index: int) -> None:
        if index == 1:
            await self._user_said(ws, TRIGGER_TEXT)
            await self._call_tool(ws, "search_code", {"pattern": "playback queue"})
        elif index == 2:
            await self._user_said(ws, INTERRUPT_TEXT)
        elif index == 3:
            await self._user_said(ws, PROBE_TEXT)
            await self._agent_says(
                ws,
                "The search found 3 matches: the voxd daemon dispatch, the "
                "playback queue append, and the provider registry.",
            )
        elif index == 4:
            await self._user_said(ws, NOTE_TEXT)
            await self._call_tool(ws, "write_note", {"text": "barge in state check"})

    async def _on_tool_result(
        self, ws: ServerConnection, message: dict[str, object]
    ) -> None:
        call_id = str(message.get("tool_call_id", ""))
        tool = self._pending.pop(call_id, "")
        if tool == "search_code":
            await self._agent_says(ws, "I found 3 matches in the code.")
        elif tool == "write_note":
            await self._agent_says(ws, "Note saved.")

    async def _call_tool(
        self, ws: ServerConnection, tool: str, params: dict[str, object]
    ) -> None:
        self._calls += 1
        call_id = f"call-{self._calls}"
        self._pending[call_id] = tool
        await self._send(
            ws,
            {
                "type": "client_tool_call",
                "client_tool_call": {
                    "tool_name": tool,
                    "tool_call_id": call_id,
                    "parameters": params,
                },
            },
        )

    async def _agent_says(self, ws: ServerConnection, text: str) -> None:
        await self._send(
            ws,
            {
                "type": "agent_response",
                "agent_response_event": {"agent_response": text},
            },
        )

    async def _user_said(self, ws: ServerConnection, text: str) -> None:
        await self._send(
            ws,
            {
                "type": "user_transcript",
                "user_transcription_event": {"user_transcript": text},
            },
        )

    @staticmethod
    async def _send(ws: ServerConnection, payload: dict[str, object]) -> None:
        await ws.send(json.dumps(payload))


async def _run() -> None:
    utterances = BargeInUtterances.synthesized(EspeakSynth())
    server = MockBargeInServer()
    async with serve(server.handle, "127.0.0.1", 0) as running:
        port = running.sockets[0].getsockname()[1]
        trace_path = _HERE / "results" / "trace_dry_run_barge_in.jsonl"
        trace = EventTrace(trace_path)
        mic = SyntheticAudio(trace)
        session = ConvAISession(
            url=f"ws://127.0.0.1:{port}",
            toolbelt=ToolBelt(_HERE / "results" / "dry_run_notes.txt"),
            trace=trace,
            overrides={},
            sink=mic,
        )
        await session.open()
        flow = BargeInFlow(session=session, mic=mic, trace=trace, utterances=utterances)
        await flow.run()
        await session.close()
    verdict = BargeInAdjudicator.from_jsonl(trace_path).adjudicate()
    print(verdict.summary())
    print(f"trace: {trace_path}")
    if verdict.verdict is not Verdict.PASSED:
        msg = f"barge-in rehearsal did not pass: {verdict.verdict}"
        raise RuntimeError(msg)
    print("dry run barge-in OK")


def main() -> None:
    """Run the offline barge-in rehearsal."""
    asyncio.run(_run())


if __name__ == "__main__":
    main()
