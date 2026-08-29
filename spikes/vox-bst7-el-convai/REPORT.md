# vox-bst7 spike report — EL Conversational AI as the E+ LLM host

**Status: BLOCKED on API key scope (pending re-run).**

The harness is complete and runs end-to-end up to the first control-plane
call, where the workspace API key is rejected:

```text
POST /v1/convai/tools -> 401 authentication_error missing_permissions:
"The API key you used is missing the permission convai_write to execute
this operation."
```

`ELEVENLABS_API_KEY` (from the repo `.envrc` / platform keychain) has
`convai_read` (GET /v1/convai/agents → 200) but not `convai_write`.
The fix is one operator action: ElevenLabs dashboard → API keys →
enable Conversational AI write on this key. Escalated to the leader
2026-08-29; every number below fills in from a re-run once the key is
rescoped.

## Offline verification (no API, no credits)

The full client pipeline was verified against a localhost mock speaking
the EL Conv AI wire protocol (`dry_run.py`), plus 35 pytest cases
covering round-trip pairing, orphaned calls, error results, trace
integrity, and percentile math:

- 3 turns, 3 tool invocations (clock, search_code, write_note), all
  paired by `tool_call_id` and closed by the next agent event.
- Measured overhead against the mock's known 350ms continuation delay:
  352-355ms — the clocks measure what they claim to measure.
- Parallel-call artifact handled: an invocation co-scheduled with our
  own slow tool is flagged `is_clean: false` and excluded from the
  gate metric (its next-event gap measures our sleep, not EL).
- Evidence: `results/trace_dry_run.jsonl`, `results/dry_run_notes.txt`.

## Verdicts (DES-069 kill criteria)

- p95 tool round-trip < 1.5s: **PENDING — blocked on key scope**
  (measurement: overall `overhead_ms` p95 from `run_automated.py`)
- barge-in mid-tool-call preserves state: **PENDING LIVE TEST**
  (procedure: README "Live run" playbook; evidence: the
  `results/trace_live_*.jsonl` event trace — `interruption`,
  `agent_response_correction`, and the post-barge-in answer to
  "what did you just find?")

## Latency tables

*(to be filled from `results/metrics_<ts>.json` — p50/p95/max for
`handling_ms`, `total_ms`, `overhead_ms`, overall and per tool)*

## Seed push (1KB / 10KB / 50KB)

*(to be filled: per size — session-start latency, first-response latency,
rejection/truncation behavior, response-quality note)*

## Environment

- Laptop: Linux x86_64, residential WAN to EL cloud (`api.elevenlabs.io`)
- Agent LLM: `gemini-2.0-flash`, temperature 0.3
- Audio: pcm_16000 both legs; automated runs use `text_only` override
- Tools: `clock` (<50ms), `search_code` (deterministic 2.2-4.3s sleep
  schedule), `write_note` (file append)
