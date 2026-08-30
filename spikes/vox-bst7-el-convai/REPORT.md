# vox-bst7 spike report — EL Conversational AI as the E+ LLM host

**Status: both kill criteria adjudicated (2026-08-29), criterion 2 by
live operator sessions with corroborating automated machine evidence.**

## Verdicts (DES-069 kill criteria)

- **p95 tool round-trip < 1.5s: PASS (measured 993ms)** — overall
  EL-attributable overhead p95 across n=27 client-tool invocations,
  real WAN, three sessions (`results/metrics_20260829T203630Z.json`).
- **barge-in mid-tool-call preserves state: PASS with caveat:
  interrupted tool's result is dropped from LLM context (deterministic,
  recoverable by re-ask); mitigation: idempotent tools + voxd result
  cache** — operator-ruled 2026-08-29. Conversation state survives a
  mid-tool-call interruption intact: no WS close, topic memory coherent,
  subsequent tools work, clean corrections — across a 7-interruption
  echo storm (`results/trace_live_20260829T224035Z.jsonl`), a 246s
  12-turn headphone session (`results/trace_live_20260829T225948Z.jsonl`,
  operator-confirmed by ear), and the automated audio-injection runs
  (`results/trace_barge_in_20260829T230811Z.jsonl`). The caveat is the
  automated runs' reproducible finding: the interrupted call's RESULT
  never reaches the LLM context (2/2 runs, including a topic-neutral
  interruption), so the agent must re-run the tool. Ruled acceptable
  with the mitigation above. Full evidence below.

## Barge-in mid-tool-call — the evidence

### Live session 1: open speakers (`trace_live_20260829T224035Z.jsonl`)

The criterion-2 event, caught in the wild: at ms=30583 the agent said
*"Let me search the code to see what language we are using."* and issued
`client_tool_call search_code` (executed 30583→32784). An `interruption`
landed at **ms=31648 — mid-tool-call** — with a clean
`agent_response_correction`, and the session carried on undamaged. EL
accepted the interrupted call's result at the transport level
(`client_tool_result` posted at 32784, `agent_tool_response` acked at
32854). Attribution caution: the correct answer that followed (*"Based
on the search results, it looks like we are using Python for this
software."*, ms=37707) came after a SECOND, echo-induced `search_code`
completed uninterrupted (33514→36614), so it cannot be cleanly credited
to the interrupted first call's result — the automated runs below are
what pin down that result's fate.

The same trace then shows 7 interruptions in a storm — and that storm
was **acoustic echo, not EL misbehavior**: the run used open speakers
and `arecord` with no echo cancellation, so the mic transcribed the
agent's own playback as user speech. Proof in the trace: `user_transcript`
events containing the agent's own prior sentences (*"Let me search the
code to see."*, repeated *"Based on the search..."*), each echo
triggering interruption → re-answer → another echo. Even so: an
echo-induced concurrent second `search_code` paired cleanly, state
stayed coherent across all 7 interruptions, and the session closed
cleanly.

### Live session 2: headphones (`trace_live_20260829T225948Z.jsonl`)

246s, 12-turn coherent conversation: 5 `search_code` calls all cleanly
paired (5 results, 5 `agent_tool_response`, zero errors), contextual
answers referencing prior tool results across turns, and 1 human
interruption at hang-up handled with a clean correction. Operator
confirmation **by ear**: *"voice quality worked well"* and *"voice,
latency, and so forth were incredibly better than what we did in
earlier spikes."*

### Corroborating machine evidence: automated audio injection

No human at the mic: `run_barge_in.py` opened a real Conv AI audio
session and streamed espeak-ng-synthesized speech (pcm_16000, paced at
real time with continuous silence, like a live mic). Script: trigger the
slow `search_code` tool (2.2s), barge in 0.2s after `client_tool_call`
arrives, ask "What did you just find?", then round-trip `write_note`.
`barge_in_verdict.py` rules four criteria from the trace JSONL; the
offline mock rehearsal (`dry_run_barge_in.py`) passed before any billed
run. Three billed runs (the budgeted cap); traces + verdict JSONs in
`results/`.

Definitive run (`trace_barge_in_20260829T230811Z.jsonl`): interruption
0.71s into the 2.2s execution window; **no WS close; session, topic
memory, and post-barge-in `write_note` all intact**; EL accepted the
mid-barge-in `client_tool_result` (`agent_tool_response` in-trace). But
asked "What did you just find?", the agent answered *"I did not find
anything because the search was stopped before it could finish."* —
**the interrupted call's result never reached the LLM context.** This
reproduced 2/2 completed runs, INCLUDING this one's topic-neutral
interruption ("Wait, wait, stop, hold on one moment…"), which rules out
"the prompt said to stop, so the LLM obeyed" as the explanation: the
drop is deterministic platform behavior, not phrasing. The loss is
recoverable — the agent offers to re-run the search, and a re-ask
succeeds — which is what the ruled mitigation (idempotent tools + a
voxd result cache, so a re-issued call is cheap) leans on. Run 2 was
INCONCLUSIVE for a separate reason worth knowing: EL's ASR clips the
onset of a session's first utterance ("Please search the code…" heard
as "Hold for the playback queue"), fixed with sacrificial lead-in
words. This is an automated synthesized-voice test of state mechanics;
the by-ear judgment is the operator's, above.

## Findings for E+ design (DES-068 inputs)

- **voxd's live audio path needs acoustic echo cancellation.**
  Full-duplex over open speakers is unusable without AEC — every agent
  sentence re-enters the mic and triggers a barge-in loop (see live
  session 1). Ship with PipeWire/PulseAudio `echo-cancel` on the capture
  leg, or document a headset assumption.
- **A barge-in during a client tool call drops that call's result from
  LLM context — deterministically.** The platform acks the result at
  the transport level but the cancelled turn takes it down; the agent
  will re-run the tool on a re-ask. Reproduced 2/2 in the automated
  runs, including with a topic-neutral interruption. E+ mitigation
  (per the operator ruling): keep client tools idempotent and add a
  voxd result cache so the re-issued call is instant and free.

## Known harness-tier issues (throwaway code, recorded only)

- `aplay` prints "underrun!!!" spam during live playback — buffer
  sizing on the raw-PCM pipe; cosmetic.
- `BrokenPipeError` race in the `aplay` flush/respawn path on barge-in
  (seen in `trace_live_20260829T224130Z.jsonl`'s session) — the writer
  can hit the killed process before the respawn lands.

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
