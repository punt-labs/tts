"""Data-plane driver: one Conv AI WebSocket session answering client tools."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, Self, final

from websockets.asyncio.client import ClientConnection, connect

from spike_tools import ToolBelt


class AudioSink(Protocol):
    """Where agent audio goes; live mode plays it, text mode discards it."""

    async def play(self, pcm: bytes) -> None:
        """Queue one PCM chunk for playback."""
        ...

    async def flush(self) -> None:
        """Drop queued audio immediately (barge-in)."""
        ...


@final
class NullAudioSink:
    """Do-nothing sink for text-only sessions."""

    __slots__ = ()

    def __new__(cls) -> Self:
        return super().__new__(cls)

    async def play(self, pcm: bytes) -> None:  # noqa: ARG002 -- Null Object, PY-DP-9
        return

    async def flush(self) -> None:
        return


# Server events that show the agent advancing the conversation. The first
# one received after a client_tool_result closes that invocation's
# round-trip clock.
_AGENT_PROGRESS_TYPES: frozenset[str] = frozenset(
    {
        "agent_response",
        "agent_chat_response_part",
        "agent_response_complete",
        "internal_tentative_agent_response",
        "audio",
        "client_tool_call",
    }
)

# Quiet period after the turn condition holds before `say` returns.
_TURN_GRACE_S = 1.5


@final
class EventTrace:
    """Append-only JSONL trace of every event the session sees or sends."""

    _path: Path
    _t0: float

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        self._path = path
        self._t0 = time.monotonic()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return self

    def record(
        self, direction: str, event_type: str, detail: Mapping[str, object]
    ) -> None:
        """Write one trace line; ``direction`` is recv, send, or note."""
        line = {
            "t": datetime.now(tz=UTC).isoformat(timespec="milliseconds"),
            "ms": round((time.monotonic() - self._t0) * 1000.0, 1),
            "dir": direction,
            "type": event_type,
            **detail,
        }
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")


@dataclass(slots=True)
class ToolInvocation:
    """Timing record of one client_tool_call round trip.

    The optional fields are lifecycle state: each is None until the
    invocation reaches that stage (executed, result sent, next agent
    event observed).
    """

    tool_name: str
    tool_call_id: str
    t_call: float
    exec_ms: float | None = None
    t_result: float | None = None
    t_next_event: float | None = None
    is_error: bool = False

    @property
    def handling_ms(self) -> float:
        """client_tool_call received -> client_tool_result sent."""
        if self.t_result is None:
            msg = f"invocation {self.tool_call_id} has no result yet"
            raise ValueError(msg)
        return (self.t_result - self.t_call) * 1000.0

    @property
    def total_ms(self) -> float:
        """client_tool_call received -> next agent progress event."""
        if self.t_next_event is None:
            msg = f"invocation {self.tool_call_id} saw no follow-up event"
            raise ValueError(msg)
        return (self.t_next_event - self.t_call) * 1000.0

    @property
    def overhead_ms(self) -> float:
        """total_ms minus local tool execution — the EL-attributable part."""
        if self.exec_ms is None:
            msg = f"invocation {self.tool_call_id} never executed"
            raise ValueError(msg)
        return self.total_ms - self.exec_ms

    @property
    def is_complete(self) -> bool:
        return self.t_next_event is not None and self.exec_ms is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "tool": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "exec_ms": round(self.exec_ms or 0.0, 1),
            "handling_ms": round(self.handling_ms, 1),
            "total_ms": round(self.total_ms, 1),
            "overhead_ms": round(self.overhead_ms, 1),
            "is_error": self.is_error,
        }


@dataclass(slots=True)
class SessionMetrics:
    """What one session measured; latencies in milliseconds."""

    ws_connect_ms: float = 0.0
    init_metadata_ms: float = 0.0
    turn_response_ms: list[float] = field(default_factory=list)
    ping_ms: list[float] = field(default_factory=list)
    invocations: list[ToolInvocation] = field(default_factory=list)
    transcript: list[dict[str, str]] = field(default_factory=list)

    @property
    def first_response_ms(self) -> float:
        if not self.turn_response_ms:
            msg = "no turns completed; first_response_ms is undefined"
            raise ValueError(msg)
        return self.turn_response_ms[0]

    @property
    def completed_invocations(self) -> list[ToolInvocation]:
        return [inv for inv in self.invocations if inv.is_complete]


@final
class ConvAISession:
    """One Conv AI conversation over a signed WebSocket URL.

    Answers ping and client_tool_call events, stamps every event into the
    trace, and accumulates :class:`SessionMetrics`.
    """

    _url: str
    _toolbelt: ToolBelt
    _trace: EventTrace
    _overrides: dict[str, object]
    _sink: AudioSink
    _ws: ClientConnection | None
    _send_lock: asyncio.Lock
    _recv_task: asyncio.Task[None] | None
    _init_event: asyncio.Event
    _executing: dict[str, ToolInvocation]
    _awaiting_next: dict[str, ToolInvocation]
    _tool_tasks: set[asyncio.Task[None]]
    _agent_responses_in_turn: int
    _turn_armed_at: float | None
    _last_progress: float
    _conversation_id: str
    _init_metadata: dict[str, object]
    _handlers: dict[str, Callable[[dict[str, object]], Coroutine[None, None, None]]]
    metrics: SessionMetrics

    def __new__(
        cls,
        *,
        url: str,
        toolbelt: ToolBelt,
        trace: EventTrace,
        overrides: Mapping[str, object],
        sink: AudioSink | None = None,  # None -> NullAudioSink (text mode)
    ) -> Self:
        self = super().__new__(cls)
        self._url = url
        self._toolbelt = toolbelt
        self._trace = trace
        self._overrides = dict(overrides)
        self._sink = sink if sink is not None else NullAudioSink()
        self._ws = None
        self._send_lock = asyncio.Lock()
        self._recv_task = None
        self._init_event = asyncio.Event()
        self._executing = {}
        self._awaiting_next = {}
        self._tool_tasks = set()
        self._agent_responses_in_turn = 0
        self._turn_armed_at = None
        self._last_progress = 0.0
        self._conversation_id = ""
        self._init_metadata = {}
        self._handlers = {
            "conversation_initiation_metadata": self._on_init_metadata,
            "ping": self._on_ping,
            "client_tool_call": self._on_client_tool_call,
            "agent_response": self._on_agent_response,
            "user_transcript": self._on_user_transcript,
            "audio": self._on_audio,
            "interruption": self._on_interruption,
            "agent_response_correction": self._on_correction,
        }
        self.metrics = SessionMetrics()
        return self

    @property
    def conversation_id(self) -> str:
        return self._conversation_id

    @property
    def init_metadata(self) -> dict[str, object]:
        return self._init_metadata

    async def open(self, *, timeout_s: float = 20.0) -> None:
        """Connect, send initiation, and wait for conversation metadata."""
        t0 = time.monotonic()
        async with asyncio.timeout(timeout_s):
            self._ws = await connect(self._url, max_size=None)
        self.metrics.ws_connect_ms = (time.monotonic() - t0) * 1000.0
        self._trace.record(
            "note", "ws_open", {"ws_connect_ms": round(self.metrics.ws_connect_ms, 1)}
        )
        t1 = time.monotonic()
        await self._send(
            {
                "type": "conversation_initiation_client_data",
                "conversation_config_override": self._overrides,
                "custom_llm_extra_body": {},
                "dynamic_variables": {},
            }
        )
        self._recv_task = asyncio.create_task(self._recv_loop())
        async with asyncio.timeout(timeout_s):
            await self._init_event.wait()
        self.metrics.init_metadata_ms = (time.monotonic() - t1) * 1000.0

    async def say(self, text: str, *, timeout_s: float = 90.0) -> str:
        """Send one user turn; return the agent's reply once the turn ends."""
        self._agent_responses_in_turn = 0
        replies_before = len(self.metrics.transcript)
        t_send = time.monotonic()
        self._turn_armed_at = t_send
        await self._send({"type": "user_message", "text": text})
        self._trace.record("send", "user_message", {"text": text})
        self.metrics.transcript.append({"role": "user", "text": text})
        deadline = t_send + timeout_s
        while True:
            if time.monotonic() > deadline:
                msg = f"turn did not complete within {timeout_s}s: {text!r}"
                raise TimeoutError(msg)
            if self._turn_is_complete():
                break
            await asyncio.sleep(0.25)
        agent_texts = [
            entry["text"]
            for entry in self.metrics.transcript[replies_before:]
            if entry["role"] == "agent"
        ]
        return " ".join(agent_texts)

    async def send_audio_chunk(self, pcm: bytes) -> None:
        """Stream one base64 PCM chunk (live mode)."""
        await self._send({"user_audio_chunk": base64.b64encode(pcm).decode()})

    async def close(self) -> None:
        for task in self._tool_tasks:
            task.cancel()
        if self._recv_task is not None:
            self._recv_task.cancel()
        if self._ws is not None:
            await self._ws.close()
        self._trace.record("note", "session_closed", {})

    # -- Receive path -----------------------------------------------------

    async def _recv_loop(self) -> None:
        if self._ws is None:
            msg = "session not opened"
            raise RuntimeError(msg)
        async for raw in self._ws:
            message = json.loads(raw)
            event_type = str(message.get("type", ""))
            if event_type in _AGENT_PROGRESS_TYPES:
                self._close_awaiting(time.monotonic())
                self._last_progress = time.monotonic()
            handler = self._handlers.get(event_type)
            if handler is not None:
                await handler(message)
            else:
                self._trace.record("recv", event_type, {})

    async def _on_init_metadata(self, message: dict[str, object]) -> None:
        event = self._event_body(message, "conversation_initiation_metadata_event")
        self._conversation_id = str(event.get("conversation_id", ""))
        self._init_metadata = event
        self._trace.record("recv", "conversation_initiation_metadata", event)
        self._init_event.set()

    async def _on_ping(self, message: dict[str, object]) -> None:
        event = self._event_body(message, "ping_event")
        ping_ms = event.get("ping_ms")
        if isinstance(ping_ms, int | float):
            self.metrics.ping_ms.append(float(ping_ms))
        await self._send({"type": "pong", "event_id": event.get("event_id")})
        self._trace.record("recv", "ping", {"ping_ms": ping_ms})

    async def _on_client_tool_call(self, message: dict[str, object]) -> None:
        event = self._event_body(message, "client_tool_call")
        name = str(event.get("tool_name", ""))
        call_id = str(event.get("tool_call_id", ""))
        params = event.get("parameters")
        params_map: Mapping[str, object] = params if isinstance(params, dict) else {}
        invocation = ToolInvocation(
            tool_name=name, tool_call_id=call_id, t_call=time.monotonic()
        )
        self._executing[call_id] = invocation
        self.metrics.invocations.append(invocation)
        self._trace.record(
            "recv", "client_tool_call", {"tool": name, "tool_call_id": call_id}
        )
        task = asyncio.create_task(self._run_tool(invocation, params_map))
        self._tool_tasks.add(task)
        task.add_done_callback(self._tool_tasks.discard)

    async def _run_tool(
        self, invocation: ToolInvocation, params: Mapping[str, object]
    ) -> None:
        loop = asyncio.get_running_loop()
        t0 = time.monotonic()
        try:
            result = await loop.run_in_executor(
                None, self._toolbelt.run, invocation.tool_name, params
            )
            is_error = False
        except (KeyError, ValueError) as exc:
            result = str(exc)
            is_error = True
        invocation.exec_ms = (time.monotonic() - t0) * 1000.0
        invocation.is_error = is_error
        await self._send(
            {
                "type": "client_tool_result",
                "tool_call_id": invocation.tool_call_id,
                "result": result,
                "is_error": is_error,
            }
        )
        invocation.t_result = time.monotonic()
        del self._executing[invocation.tool_call_id]
        self._awaiting_next[invocation.tool_call_id] = invocation
        self._trace.record(
            "send",
            "client_tool_result",
            {
                "tool": invocation.tool_name,
                "tool_call_id": invocation.tool_call_id,
                "exec_ms": round(invocation.exec_ms, 1),
                "is_error": is_error,
            },
        )

    async def _on_agent_response(self, message: dict[str, object]) -> None:
        event = self._event_body(message, "agent_response_event")
        text = str(event.get("agent_response", "")).strip()
        self.metrics.transcript.append({"role": "agent", "text": text})
        self._agent_responses_in_turn += 1
        if self._turn_armed_at is not None and self._agent_responses_in_turn == 1:
            elapsed = (time.monotonic() - self._turn_armed_at) * 1000.0
            self.metrics.turn_response_ms.append(elapsed)
        self._trace.record("recv", "agent_response", {"text": text})

    async def _on_user_transcript(self, message: dict[str, object]) -> None:
        event = self._event_body(message, "user_transcription_event")
        text = str(event.get("user_transcript", "")).strip()
        self.metrics.transcript.append({"role": "user_transcript", "text": text})
        self._trace.record("recv", "user_transcript", {"text": text})

    async def _on_audio(self, message: dict[str, object]) -> None:
        event = self._event_body(message, "audio_event")
        audio_b64 = str(event.get("audio_base_64", ""))
        self._trace.record(
            "recv",
            "audio",
            {"bytes_b64": len(audio_b64), "event_id": event.get("event_id")},
        )
        if audio_b64:
            await self._sink.play(base64.b64decode(audio_b64))

    async def _on_interruption(self, message: dict[str, object]) -> None:
        event = self._event_body(message, "interruption_event")
        self._trace.record("recv", "interruption", event)
        await self._sink.flush()

    async def _on_correction(self, message: dict[str, object]) -> None:
        event = self._event_body(message, "agent_response_correction_event")
        self._trace.record("recv", "agent_response_correction", event)

    # -- Internals ---------------------------------------------------------

    def _turn_is_complete(self) -> bool:
        if self._agent_responses_in_turn < 1 or self._executing:
            return False
        quiet_for = time.monotonic() - self._last_progress
        return quiet_for >= _TURN_GRACE_S

    def _close_awaiting(self, now: float) -> None:
        for invocation in self._awaiting_next.values():
            invocation.t_next_event = now
        self._awaiting_next.clear()

    async def _send(self, payload: Mapping[str, object]) -> None:
        if self._ws is None:
            msg = "session not opened"
            raise RuntimeError(msg)
        async with self._send_lock:
            await self._ws.send(json.dumps(payload))

    @staticmethod
    def _event_body(message: dict[str, object], key: str) -> dict[str, object]:
        body = message.get(key)
        return dict(body) if isinstance(body, dict) else {}
