# vox-bst7 — ElevenLabs Conversational AI foundation spike

Validates DES-069 (E+ voice-agent LLM turn loop) against its two kill
criteria:

1. **Client-tool round-trip latency** under real WAN conditions — kill if
   overall p95 ≥ 1.5s.
2. **Barge-in mid-tool-call** — kill if interrupting the agent while a
   client tool is running corrupts conversation state.

Throwaway spike code: nothing here ships, nothing touches `src/`. Scripts
are self-contained PEP 723 uv scripts; run them **from this directory**
so sibling imports resolve.

## Files

| File | Role |
|------|------|
| `spike_tools.py` | The 3 client tools: `clock` (fast, <50ms), `search_code` (slow, 2-5s simulated), `write_note` (persists to `notes.txt`) |
| `control_plane.py` | REST client: create/delete tools + agent, signed URL |
| `convai.py` | Data plane: WebSocket session, tool dispatch, event trace, latency records |
| `seed.py` | Deterministic session-context seed text generator |
| `setup_agent.py` | Creates the EL agent + tools; writes `agent.json` |
| `run_automated.py` | Criteria a/b/c/f: text-mode conversations, latency metrics, seed push |
| `run_live.py` | Criteria d/e: live mic/speaker call with event tracing |
| `teardown_agent.py` | Deletes the agent + tools |
| `results/` | Event traces (JSONL) and metrics (JSON) |

## Prerequisites

- `ELEVENLABS_API_KEY` in the environment (the repo `.envrc` provides it)
  with **Conv AI read + write** permissions (`convai_read`, `convai_write`).
  A TTS-only key fails fast with a one-line 401.
- Linux live mode needs `arecord`/`aplay` (alsa-utils).

## Automated runs (no human audio)

```sh
cd spikes/vox-bst7-el-convai
direnv exec ../../ uv run setup_agent.py        # once
direnv exec ../../ uv run run_automated.py --smoke   # 1 short run, sanity
direnv exec ../../ uv run run_automated.py           # 3 seed sizes, full
```

The full run opens one text-only session per seed size (1KB / 10KB / 50KB
of generated session context pushed as a prompt override), drives 7
scripted turns per session (~9 tool invocations each), and answers every
`client_tool_call` locally. Output:

- `results/trace_<tag>.jsonl` — every WS event, timestamped.
- `results/metrics_<ts>.json` — per-run session-start / first-response
  latencies and per-invocation timings, plus p50/p95/max aggregates.
- A latency table and the gate verdict on stdout.

### What the latency numbers mean

Per invocation, from the client's clock:

- `exec_ms` — local tool body runtime (known schedule for `search_code`).
- `handling_ms` — `client_tool_call` received → `client_tool_result` sent.
- `total_ms` — `client_tool_call` received → next agent progress event.
- `overhead_ms` — `total_ms − exec_ms`: the EL-attributable round trip
  (WAN + orchestrator + LLM continuation). **The 1.5s gate applies to
  this** — the raw `total_ms` of the slow tool contains our own 2-5s
  sleep by design. Aggregated over *clean* invocations only (ones whose
  result was the last the agent waited on); when the agent runs two of
  our tools in parallel, the faster tool's next-event gap measures our
  own slow tool, not EL, and is excluded (`is_clean: false` in the
  metrics JSON).

## Live run (operator + leader, after the mission closes)

```sh
cd spikes/vox-bst7-el-convai
direnv exec ../../ uv run setup_agent.py   # if agent.json is absent (post-teardown)
direnv exec ../../ uv run run_live.py
```

Playbook, one step per test:

1. **Warm-up turn-taking**: say hello; interrupt the agent mid-sentence.
   Expect playback to cut instantly and an `interruption` +
   `agent_response_correction` pair in the trace.
2. **Barge-in during a tool call**: say "search the code for the playback
   queue" — the slow tool holds the call open for 2-5s — and start
   talking before the agent answers. Then ask "what did you just find?"
   The follow-up answer is the state-integrity check: if the agent has
   lost or mangled the tool context, barge-in corrupted state.
3. **Note round trip**: ask it to write a note; verify `notes.txt` gained
   the line.

Ctrl-C hangs up and prints trace counts (tool calls, interruptions,
corrections). The trace JSONL is the machine evidence for the
"barge-in mid-tool-call preserves state" verdict in `REPORT.md`.

## Teardown

```sh
direnv exec ../../ uv run teardown_agent.py
```

Conv AI usage bills real credits — tear down when the spike closes and
keep automated runs to the scripted minimum.
