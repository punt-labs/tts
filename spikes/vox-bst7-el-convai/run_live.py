# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx>=0.27", "websockets>=14"]
# ///
"""Live-mic entry point for the barge-in / turn-taking session (criteria d, e).

Run from this directory on a machine with a microphone and speakers:

    direnv exec ../../ uv run run_live.py

Talk to the agent. To exercise the two live tests:

1. Barge-in during a tool call: ask it to "search the code for the playback
   queue" (the slow tool runs 2-5s), then start talking BEFORE it answers.
2. Plain turn-taking: interrupt it mid-sentence during a normal reply.

Every event is stamped into results/trace_live_<ts>.jsonl -- turn
boundaries (user_transcript / agent_response), tool call start/end,
interruption events, and agent_response_correction (the conversation
state before/after the barge-in). Ctrl-C ends the call and prints a
summary of that machine evidence.

Requires ALSA's arecord/aplay (Linux). Audio format is pcm_16000 on both
legs, matching the agent config written by setup_agent.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Self, final

from control_plane import AgentHandle, ControlPlane
from convai import ConvAISession, EventTrace
from spike_tools import ToolBelt

_HERE = Path(__file__).parent
_RESULTS = _HERE / "results"
_RATE = 16_000
_CAPTURE_CHUNK = 2_048  # bytes of S16LE mono @16k -- 64ms per send


@final
class AlsaAudio:
    """Mic capture and speaker playback via arecord/aplay subprocesses.

    Satisfies the session's AudioSink protocol: agent audio is queued to
    an aplay writer; ``flush`` kills and respawns aplay so barge-in cuts
    playback immediately.
    """

    _trace: EventTrace
    _playback: subprocess.Popen[bytes] | None
    _queue: asyncio.Queue[bytes]
    _writer: asyncio.Task[None] | None
    _capture: asyncio.subprocess.Process | None

    def __new__(cls, trace: EventTrace) -> Self:
        for binary in ("arecord", "aplay"):
            if shutil.which(binary) is None:
                msg = f"{binary} not found -- install alsa-utils"
                raise RuntimeError(msg)
        self = super().__new__(cls)
        self._trace = trace
        self._playback = None
        self._queue = asyncio.Queue()
        self._writer = None
        self._capture = None
        return self

    async def start(self, session: ConvAISession) -> None:
        """Spawn playback + capture; stream mic chunks into the session."""
        self._playback = self._spawn_playback()
        self._writer = asyncio.create_task(self._writer_loop())
        self._capture = await asyncio.create_subprocess_exec(
            "arecord",
            "-q",
            "-f",
            "S16_LE",
            "-r",
            str(_RATE),
            "-c",
            "1",
            "-t",
            "raw",
            stdout=asyncio.subprocess.PIPE,
        )
        assert self._capture.stdout is not None  # noqa: S101 -- PIPE guarantees a stream; internal invariant
        while True:
            chunk = await self._capture.stdout.read(_CAPTURE_CHUNK)
            if not chunk:
                break
            await session.send_audio_chunk(chunk)

    async def play(self, pcm: bytes) -> None:
        await self._queue.put(pcm)

    async def flush(self) -> None:
        """Cut playback now: drop the queue and restart aplay."""
        while not self._queue.empty():
            self._queue.get_nowait()
        # Reassign BEFORE kill -- _write_chunk's death-check disambiguation
        # depends on it. A kill-induced BrokenPipeError must only be
        # observable after `self._playback is not playback` holds, or a
        # concurrent writer classifies a normal barge-in as aplay dying
        # (false aplay_died in the trace) and respawns a second aplay,
        # orphaning whichever Popen loses the assignment race.
        old = self._playback
        self._playback = self._spawn_playback()
        if old is not None:
            old.kill()

    async def stop(self) -> None:
        if self._writer is not None:
            self._writer.cancel()
        if self._capture is not None:
            with contextlib.suppress(ProcessLookupError):
                self._capture.kill()
        if self._playback is not None:
            self._playback.kill()

    @staticmethod
    def _spawn_playback() -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [
                "aplay",
                "-q",
                "-f",
                "S16_LE",
                "-r",
                str(_RATE),
                "-c",
                "1",
                "-t",
                "raw",
            ],
            stdin=subprocess.PIPE,
        )

    async def _writer_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            chunk = await self._queue.get()
            await loop.run_in_executor(None, self._write_chunk, chunk)

    def _write_chunk(self, chunk: bytes) -> None:
        playback = self._playback
        if playback is None or playback.stdin is None:
            return
        try:
            playback.stdin.write(chunk)
            playback.stdin.flush()
        except (BrokenPipeError, ValueError):
            if self._playback is not playback:
                # Barge-in flush respawned aplay mid-write; drop the chunk.
                return
            if playback.poll() is None:
                return  # pipe hiccup but the process lives; drop the chunk
            # aplay died for real: without a respawn every later chunk
            # drops forever and the operator hears silence, misjudging
            # the session. Record it and bring playback back.
            rc = playback.returncode
            self._trace.record("note", "aplay_died", {"returncode": rc})
            print(f"aplay died (rc={rc}); respawning", file=sys.stderr)
            self._playback = self._spawn_playback()


def _summarize(trace_path: Path) -> str:
    """Count the barge-in evidence lines in the trace."""
    counts = {"client_tool_call": 0, "interruption": 0, "agent_response_correction": 0}
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        # Each line is one JSON object; only ITS type counts. A substring
        # match would tally event shapes nested inside recorded bodies.
        event_type = str(json.loads(line).get("type", ""))
        if event_type in counts:
            counts[event_type] += 1
    return (
        f"tool calls: {counts['client_tool_call']}, "
        f"interruptions: {counts['interruption']}, "
        f"corrections: {counts['agent_response_correction']}"
    )


async def _run() -> Path:
    handle = AgentHandle.load(_HERE / "agent.json")
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    trace_path = _RESULTS / f"trace_live_{stamp}.jsonl"
    trace = EventTrace(trace_path)
    plane = ControlPlane()
    try:
        url = plane.signed_url(handle.agent_id)
    finally:
        plane.close()
    audio = AlsaAudio(trace)
    session = ConvAISession(
        url=url,
        toolbelt=ToolBelt(_HERE / "notes.txt"),
        trace=trace,
        overrides={},
        sink=audio,
    )
    await session.open()
    print(f"connected: conversation {session.conversation_id}")
    print(f"trace:     {trace_path}")
    print("talk now — Ctrl-C to hang up")
    t0 = time.monotonic()
    try:
        await audio.start(session)
    finally:
        elapsed = time.monotonic() - t0
        await audio.stop()
        await session.close()
        print(f"\ncall length: {elapsed:.0f}s")
        print(f"evidence:    {_summarize(trace_path)}")
        print(f"trace:       {trace_path}")
    return trace_path


def main() -> None:
    """Run the live call until Ctrl-C."""
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("call ended")


if __name__ == "__main__":
    main()
