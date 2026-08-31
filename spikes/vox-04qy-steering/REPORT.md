# vox-04qy — Steering a Running Mode B Session: Findings

**Bead:** vox-04qy (P1). **Mission:** m-2026-08-31-004.
**Question:** can an external process deliver a mid-run user turn into a
launched Mode B session, with confirmed receipt and characterized mid-turn
semantics? Two channels, one arm each, per `PLAN.md`.

**Both arms PASS.** In both channels a steering input delivered mid-turn
reached the agent with a confirmed, committed receipt, and the mid-turn
semantics are characterized below with the evidence files that back each
claim. DES-068 Wall 2 ("no background process can inject") is contradicted
by both arms for the agents tested: pi has a first-class protocol verb for
it, and Claude Code's TTY accepts mid-turn input, queueing it into the
running prompt for the model's next inference boundary.

## Arm 1 — pi RPC `steer` verb: PASS

Harness: `run_arm1.py` over `rpc_session.py`/`rpc_protocol.py` —
`pi 0.84.4`, `--mode rpc --no-session --no-extensions`, provider
`anthropic`, model `claude-haiku-4-5`, tools pinned read-only
(`read,grep,find,ls`), scratch cwd outside the repo, sentinel stubs first
on PATH (zero hits both runs). Two full runs, differing only in the steer
phrasing: `results/arm1-plain/` and `results/arm1-adversarial/` (three
sanitized transcripts + `summary.json` each).

### Mid-turn semantics (explicit)

- **Queue, never interrupt.** `steer` acks in **1.4 ms** (plain run;
  5.1 ms adversarial) with a `queue_update` showing the text in the
  `steering` queue. Nothing already in flight is aborted.
- **In-flight tool-call fate: always completes.** The read that was
  running when steer landed finished normally **2.3 ms** later (plain;
  6.4 ms adversarial) — `in_flight_tool.completed_after_steer: true` in
  both summaries. The bst7 barge-in question, answered on the pi side:
  no tool call is killed.
- **Injection point: the next turn boundary.** At the in-flight tool's
  `turn_end`/`turn_start` seam — 2.7 ms after send in the plain run,
  7.1 ms in the adversarial — the queue drains and the steer text enters
  the conversation as a user message.
  Evidence: `arm1-plain/midturn_steer.transcript.jsonl` — send `steer`,
  then `queue_update`, `tool_execution_end`, `turn_end`, `turn_start`,
  user `message_start` carrying the steer text.
- **Stream coherence after: clean.** The event stream continues
  `turn_start → assistant message → turn_end → agent_end → agent_settled`
  with no malformed frames in any of the six scenario transcripts.
- **Latency to first steered output:** assistant began answering the
  steer **877 ms** after steer-write (plain run), with **zero** further
  tool calls (`tool_starts_after_steer: 0`) and `STEERED-ACK` as the
  final message.
- **Idle steer parks.** With no turn running, `steer` acks and queues but
  starts nothing (`idle_steer_started_a_turn: false`, both runs); the
  queued text delivers together with the *next* `prompt` — both acks then
  arrive in one assistant reply. A steering channel built on this must
  either know the agent is mid-turn or pair idle-steer with a prompt.
- **`follow_up` contrast: end-of-task, not next-boundary.** `follow_up`
  acks in ~2 ms into the separate `followUp` queue but delivers only
  after the whole current task finishes — `FOLLOWUP-ACK` at **+13.9 s**
  (task ran all six files) versus steer's next-boundary +0.9 s. One agent
  loop: the follow-up ran as an extra turn before `agent_end`. `steer` is
  the steering verb; `follow_up` is "and afterwards".

### The channel and the model are separate questions

The first run phrased the steer as a protocol frame — `URGENT STEER
vox04qy: stop the summaries immediately...`. The channel delivered it
mid-turn in 7 ms; the model (haiku) then **refused to comply**, reasoning
in its thinking block that the message looked like a prompt-injection
test, and completed all six file summaries anyway
(`tool_starts_after_steer: 5`, `arm1-adversarial/midturn_steer.transcript.jsonl`).
The identical request phrased as a plain user sentence ("Actually, please
stop — I don't need the rest...") complied in 877 ms. **Steering text must
read like the user talking, not like a control protocol** — that is a
design constraint for whatever composes the steer message (the voice
pipeline), not a channel limitation.

## Arm 2 — claude TUI in tmux via send-keys: PASS

Harness: `run_arm2.py` over the copied juhw/73y7 fork machinery — claude
2.1.251 forked into a detached tmux session, scratch project + fresh
`CLAUDE_CONFIG_DIR` under `~/.cache/vox04qy-scratch` (outside the repo),
`steer-inject-v1` profile (no Bash), all hook events relayed
sender-stamped to the stub store. **Receipt = the injected text's
`UserPromptSubmit` record in the committed ledger**
(`results/arm2/hook_ledger.jsonl`), pane captures as secondary witness.
One fork, five cases, all five received (`results/arm2/summary.json`):

| Case | send→hook-visible | Receipt | Semantics |
|---|---|---|---|
| idle inject | 564 ms | `recv_seq 6` | Submits at the boundary; `IDLEACK` reply (pane) |
| mid-turn inject | 561 ms | `recv_seq 11` | **Queued into the running prompt; delivered at the next inference boundary** — see below |
| Esc-then-steer | 572 ms | `recv_seq 20` | Esc interrupts **without a Stop hook**; replacement starts a fresh turn |
| bracketed paste | 1092 ms (incl. 1 s settle) | `recv_seq 22` | Three lines land as **one** prompt |
| literal `-l` send | 587 ms | `recv_seq 24` | Flags/quotes/keynames arrive **byte-for-byte verbatim** |

The ~500 ms of each send→hook-visible figure is the fixed hook-dispatch
pipeline (send-keys itself is instant; hook-fire→store is 47–75 ms of it,
per the `hook_fire_to_store_ms` relay stamps); the delivery is effectively
immediate at voice timescales.

### Mid-turn semantics (explicit)

- **Mid-turn typed input is QUEUED — not dropped, not an interrupt.**
  Claude Code accepts the text into the running prompt's queue (its TUI
  labels such input "queued messages"), and the receipt proves the
  attachment: the injected `UserPromptSubmit` carries the SAME
  `prompt_id` as the original task submission — `recv_seq 11` shares
  `prompt_id 0d29444f…` with `recv_seq 8` in the committed ledger, while
  every boundary injection got a fresh id. The hook
  receipt landed **561 ms after send-keys, while the turn was running**
  (between PostToolUse 10 and PreToolUse 12); the model incorporated it
  at its next inference boundary, **ended the task early** — it read
  `notes_02`, wrote `summary_02`, and stopped; summaries 03–06 were
  never written — and replied `MIDACK-vox04qy` (pane_esc_pressed.txt
  scrollback). The same queue-then-inject shape as pi's `steer`, with
  in-flight tool calls unharmed.
- **Escape is the hard steer, and it is silent.** Esc killed the running
  task turn with **no Stop hook fired** (`stops_before_escape == 3 ==
  stops_before_delivery`) — a steering daemon must not wait for a Stop
  to conclude an interrupt landed. The replacement text submitted 572 ms
  later, under a fresh `prompt_id`, and was answered (`ESCACK-vox04qy`).
- **Mechanics all hold.** `send-keys` text + separate settle + `Enter`
  submits reliably (five for five); `paste-buffer -p` (bracketed paste)
  keeps a multi-line message one message; `-l --` literal mode preserves
  text that would otherwise be parsed as flags or key names
  (`text_arrived_verbatim: true`). No TUI redraw races were observed at
  these settle times (0.5–1.0 s).
- **Zero dialog nudges were needed** (the seeded config pre-accepted
  trust/onboarding), **zero sentinel-stub hits** — an observation, not a
  default: the invocation log is created empty at setup, harvested
  *before* teardown removes it, and a missing log raises rather than
  reading as clean — and teardown verified clean twice (`teardown.log`),
  with the pgrep evidence pass recorded before any kill.

## What this means for DES-068 Wall 2

"No background process can inject" does not hold as stated:

- **pi**: `steer` is a supported protocol verb over the daemon-held
  process's stdin — the recommended channel, with queue-at-boundary
  semantics and millisecond acks.
- **claude**: the TTY *is* an injection surface. tmux send-keys yields
  confirmed, receipt-backed, mid-turn delivery with the same
  queue-then-inject semantics as pi's steer (queued into the running
  prompt, incorporated at the next boundary, early wrap-up), plus Escape
  as the hard interrupt.
  The channel needs the session to live in a harness-owned tmux pane —
  exactly what Mode B's launcher already does.

Recommended channel per agent type, for the DES-073 draft: pi → RPC
`steer` (contrast `follow_up` for deferred instructions); claude → tmux
send-keys with the hook store as the delivery receipt, Escape-then-steer
for hard redirection. Both channels demand **user-voiced phrasing** of
the steer text (Arm 1's refusal finding applies to any model).

## Caveats

- Model compliance is phrasing- and model-dependent (haiku refused the
  protocol-styled steer; claude 2.1.251 stopped one file late on the
  "stop after the current file" request). The channel guarantees
  *delivery*, not obedience.
- Arm 2's `UserPromptSubmit` receipt proves submission, and the pane
  captures prove the reply; there is no per-token stream on this channel,
  so fine-grained injection timing inside a single inference call is not
  observable from outside.
- One store-side `ConnectionClosedError` was logged when a relay client
  dropped without a close frame (mcp-proxy hitting its timeout); no
  ledger record was lost (relay sequences 1–26 are contiguous).
- The first live run's teardown raced the dying fork's exit flush: claude
  recreated an **empty** `claude-config/` skeleton (zero files, no
  credentials) after `rmtree`, so "clean" was reported with a bare
  directory left behind. The runner now waits for the pane process to
  die and settle before removing the scratch root; `teardown.py` remains
  the idempotent backstop.
- The store APPENDS to its ledger, so a rerun over an existing
  `results/arm2/hook_ledger.jsonl` would interleave runs and satisfy
  receipt waits with the previous run's records (observed once as
  negative latencies before the guard existed). `run_arm2.py` now
  refuses to start over an existing ledger; move the old run aside
  first. The committed `results/arm2/` is one clean single-session run.

## Reproducing

```sh
cd spikes/vox-04qy-steering
direnv exec ../../ uv run pytest .                       # offline pins
direnv exec ../../ uv run run_arm1.py --steer-style plain
direnv exec ../../ uv run run_arm1.py --steer-style adversarial
direnv exec ../../ uv run run_arm2.py
direnv exec ../../ uv run teardown.py                    # idempotent
```
