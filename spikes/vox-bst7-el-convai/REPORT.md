# vox-bst7 spike report — EL Conversational AI as the E+ LLM host

**Status: BLOCKED on API key scope (pending re-run).**

The harness is complete and runs end-to-end up to the first control-plane
call, where the workspace API key is rejected:

```text
POST /v1/convai/tools -> 401 authentication_error missing_permissions:
"The API key you used is missing the permission convai_write to execute
this operation."
```

`ELEVENLABS_API_KEY` (from the repo `.envrc` / platform keychain) is
scoped for TTS only. The spike needs `convai_read` + `convai_write`
(ElevenLabs dashboard → API keys). Escalated to the leader 2026-08-29;
every number below fills in from a re-run once the key is rescoped.

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
