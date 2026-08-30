# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx>=0.27", "websockets>=14"]
# ///
"""Automated barge-in state-integrity run (DES-069 kill criterion 2).

Opens a REAL Conv AI audio session against the live spike agent and
injects pre-synthesized user speech (espeak-ng, pcm_16000) through the
SyntheticAudio seam: trigger the slow ``search_code`` tool, barge in
while it is still executing, ask "what did you just find?", then
round-trip ``write_note``. The trace JSONL is the machine evidence; the
adjudicator prints and saves the four-criterion verdict.

This is an automated synthesized-voice test -- it rules on STATE
integrity only. Audio UX (how the barge-in sounds) still needs the
operator's ear. Each run bills real credits: run it deliberately.

    direnv exec ../../ uv run run_barge_in.py
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from barge_in import BargeInFlow, BargeInUtterances, SyntheticAudio
from barge_in_verdict import BargeInAdjudicator
from control_plane import AgentHandle, ControlPlane
from convai import ConvAISession, EventTrace
from speech import EspeakSynth
from spike_tools import ToolBelt

_HERE = Path(__file__).parent
_RESULTS = _HERE / "results"

# The whole scripted conversation fits well inside this; a hung session
# must not keep billing.
_RUN_TIMEOUT_S = 180.0


async def _run() -> None:
    # Everything local happens before the billed session opens.
    handle = AgentHandle.load(_HERE / "agent.json")
    utterances = BargeInUtterances.synthesized(EspeakSynth())
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    trace_path = _RESULTS / f"trace_barge_in_{stamp}.jsonl"
    trace = EventTrace(trace_path)
    plane = ControlPlane()
    try:
        url = plane.signed_url(handle.agent_id)
    finally:
        plane.close()
    mic = SyntheticAudio(trace)
    session = ConvAISession(
        url=url,
        toolbelt=ToolBelt(_HERE / "notes.txt"),
        trace=trace,
        overrides={},
        sink=mic,
    )
    await session.open()
    print(f"connected: conversation {session.conversation_id}")
    print(f"trace:     {trace_path}")
    flow = BargeInFlow(session=session, mic=mic, trace=trace, utterances=utterances)
    try:
        async with asyncio.timeout(_RUN_TIMEOUT_S):
            await flow.run()
    finally:
        await session.close()
    verdict = BargeInAdjudicator.from_jsonl(trace_path).adjudicate()
    verdict_path = _RESULTS / f"verdict_barge_in_{stamp}.json"
    verdict_path.write_text(
        json.dumps(verdict.as_dict(), indent=2) + "\n", encoding="utf-8"
    )
    print()
    print(verdict.summary())
    print(f"verdict: {verdict_path}")


def main() -> None:
    """Run the billed audio-injection test once."""
    asyncio.run(_run())


if __name__ == "__main__":
    main()
