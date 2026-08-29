# vox-bst7 spike report — EL Conversational AI as the E+ LLM host

**Status: automated criteria measured (2026-08-29). Live barge-in test
pending, by design — it needs the operator.**

## Verdicts (DES-069 kill criteria)

- **p95 tool round-trip < 1.5s: PASS (measured 993ms)** — overall
  EL-attributable overhead p95 across n=27 client-tool invocations,
  real WAN, three sessions (`results/metrics_20260829T203630Z.json`).
- **barge-in mid-tool-call preserves state: PENDING LIVE TEST** —
  procedure: README "Live run" playbook (`run_live.py`); evidence to
  collect: the `results/trace_live_*.jsonl` event trace — `interruption`
  and `agent_response_correction` events around an in-flight
  `search_code` call, plus the agent's post-barge-in answer to "what did
  you just find?" as the state-integrity check.

## Latency tables (full run, 3 sessions × 7 turns, 27 invocations)

All milliseconds; nearest-rank percentiles. `overhead_ms` =
`total_ms − exec_ms`, aggregated over *clean* invocations only (result
was the last thing the agent waited on).

```text
metric                         n       p50       p95       max
--------------------------------------------------------------
overall handling_ms           27         1      4301      4301
overall total_ms              27       812      4451      4484
overall overhead_ms           27       150       993       993
clock handling_ms              9         1         2         2
clock total_ms                 9        18       128       128
clock overhead_ms              9        17       127       127
search_code handling_ms        9      3101      4301      4301
search_code total_ms           9      3853      4484      4484
search_code overhead_ms        9       675       993       993
write_note handling_ms         9         1         4         4
write_note total_ms            9       812       993       993
write_note overhead_ms         9       811       993       993
```

Reading notes:

- `search_code`'s `handling_ms`/`total_ms` include our own deliberate
  2.2-4.3s sleep; its **overhead** (675-993ms) is the honest
  EL continuation cost after a slow tool.
- `clock`'s tiny overhead (p50 17ms, below even the ping RTT) shows the
  metric can *undershoot* when the agent already has an event in flight
  (pre-tool speech / tentative response) — so the gate is conservative
  in the direction that matters: the worst honest number (993ms) is
  still under the 1.5s kill line by a third.
- WebSocket ping RTT observed across the sessions: 64-254ms.
- 27/27 invocations completed; 0 orphans; 0 tool errors.

## Seed push (prompt override at session start)

| seed | prompt bytes | signed URL | WS connect | init metadata | first response | rejected/truncated |
|------|-------------|-----------|-----------|--------------|----------------|--------------------|
| 1KB  | 1,597  | 338ms | 553ms | 0.3ms | 130ms | no |
| 10KB | 10,813 | 461ms | 266ms | 0.6ms | 409ms | no |
| 50KB | 51,773 | 551ms | 334ms | 1.3ms | 141ms | no |

- **EL accepted the full 50KB prompt override with no rejection, no
  truncation, no connect penalty** — session-start latency shows no
  size correlation (it is dominated by TLS/WS setup, 267-553ms).
- Response quality: at 1KB and 10KB the agent consistently answered
  with tool *results* ("I found three matches for the playback
  queue..."). At 50KB it leaned into pre-tool narration — several turns
  closed on "Searching the codebase for X." without the result summary,
  and the turn-4 reply narrated both intents but neither result. Mild
  but real degradation: with a large seed, gemini-2.0-flash gets
  chattier about intent and lazier about folding tool output back in.
  Context for E+: DES-070's Layer-1 seed can be generous (50KB is fine
  transport-wise), but prompt discipline matters more as it grows.

## Environment

- Laptop: Linux x86_64, residential WAN to EL cloud
  (`api.elevenlabs.io`); WS ping RTT 64-254ms during the runs.
- Agent: `gemini-2.0-flash`, temperature 0.3, EL client tools with
  `expects_response: true`, `response_timeout_secs: 20`, default
  interruption mode (`allow` — barge-in during tool calls stays enabled
  for the live test).
- Audio: pcm_16000 both legs; automated runs used the
  `conversation.text_only` override (no TTS credits burned; LLM +
  platform usage only).
- Tools: `clock` (<50ms), `search_code` (deterministic 2.2-4.3s sleep
  schedule so exec time subtracts exactly), `write_note` (file append,
  `notes.txt` — 9 notes persisted across the runs).
- Protocol: no mismatches vs. the EL docs/SDK-derived shapes — the
  harness ran against the real WebSocket unchanged from the mock-tested
  build (same init handshake, `client_tool_call`/`client_tool_result`,
  ping/pong, `agent_response`).
- One control-plane quirk: agent deletion propagates lazily — deleting
  a tool right after its agent returns 409 "still in use"; the teardown
  retries with `?force=true` and treats 404 as already-gone so re-runs
  are idempotent. All spike agents/tools were removed after the runs.

## Offline verification (kept for provenance)

Before the first billed call, the pipeline was verified against a
localhost mock speaking the EL wire protocol (`dry_run.py`) plus a
35-case offline pytest suite (round-trip pairing by `tool_call_id`,
duplicate-id dedup, orphaned calls, error results, trace-stamp
integrity, `_summarize` JSON parsing, percentile math, zero-first-
response truthiness). Measured mock overhead 352-355ms against a known
350ms delay — the clocks measure what they claim. Evidence:
`results/trace_dry_run.jsonl`.

## History

- 2026-08-29 (round 1): blocked — API key had `convai_read` but not
  `convai_write` (`POST /v1/convai/tools` → 401 missing_permissions).
  Escalated; operator rescoped the key same day, unblocking round 2.
