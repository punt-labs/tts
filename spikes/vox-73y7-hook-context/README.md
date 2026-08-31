# vox-73y7 — hook-fanout context spike

Adjudicates DES-070 (DES-068 umbrella): is continuous hook fanout a
load-bearing context feed for the voice agent, or does a `/vox:talk`
seed alone suffice? Four questions, per the bead:

- **(a) realism** — under a real working session (multi-file task with a
  test-failure/debug/fix loop), what STATE do hook payloads carry, per
  event type, vs bare metadata?
- **(b) latency** — hook fire to store-visible, p50/p95/max.
- **(c) gaps** — kill the store mid-session, lose events, restart: do
  per-session monotonic sequences detect and quantify the loss?
- **(d) seed** — a bounded ~10KB hand-picked seed from the same ledger:
  does it answer "what was I just doing?" as well as the raw tail?

Throwaway spike code: nothing here ships, nothing touches `src/`. Scripts
are self-contained PEP 723 uv scripts; run them **from this directory** so
sibling imports resolve.

## The chain under test

```text
StoreProcess (stub voxd)      SessionLauncher                claude fork
ws://127.0.0.1:<port>  <---   tmux new-session -d      --->  scratch project
        ^                     -e CLAUDE_CONFIG_DIR=...       seeded failing suite
        |                                                    .claude/settings.json
        +--- relay.sh: python3 relay_stamp.py | mcp-proxy ---+  ALL hook events
             (sender-side relay_seq + relay_start_ns)
```

The receiving stack is the vox-juhw store (four review rounds of
hardening: recursive redaction, path sanitization, torn-line-tolerant
snapshot reads), copied and adapted — see each module docstring for the
delta. The new piece is **sender-side stamping**: `relay_stamp.py` injects
a per-session `relay_seq` (flock-guarded counter file) and a
`relay_start_ns` wall-clock stamp into every payload before `mcp-proxy`
relays it. Receiver-assigned sequences cannot detect drops — events lost
while the store is down were never received, so receiver sequences stay
contiguous by construction. Loss detection needs the sender-side counter;
that asymmetry is itself a spike finding for DES-070.

## Files

| File | Role |
|------|------|
| `stamp.py` | `SequenceStamper`/`HookRecord`/`HookLedger` (juhw, + `received_ns`) |
| `hook_store.py` | Stub voxd context store (juhw, local `stamp` import) |
| `relay_stamp.py` | Sender-side stamper: `relay_seq` + `relay_start_ns` |
| `wiring.py` | `context-capture-v1` profile, relay-script rendering, ALL-events hook wiring |
| `scratch.py` | Scratch project + isolated config (juhw, + task seeding, relay deposit) |
| `launcher.py` | tmux fork, cap 2 (juhw, prefix `vox73y7`) |
| `session_task.py` | Seeded buggy `textstat` project + bounded task prompt |
| `field_inventory.py` | Question (a): per-event field census + byte distributions |
| `latency.py` | Question (b): fire-to-visible latency, p50/p95/max |
| `gap_check.py` | Question (c): sender-seq gap detection + receiver-seq contrast |
| `reconstructor.py` | Verdict core: deterministic ledger-tail "what was I just doing?" |
| `seed_builder.py` | Question (d): bounded ~10KB seed + seed-only reconstruction |
| `dry_run.py` | Offline rehearsal: synthetic ledger + real-wire leg, no forks |
| `run_capture.py` | The ONE live capture run: fork, sample, gap, analyze |
| `teardown.py` | Idempotent teardown (tmux + scratch root) |
| `results/` | Committed evidence per run (`run_<ts>/`, `el_retest/`) |
| `REPORT.md` | Acceptance verdict in the bead's terms |

`test_*.py` files are written by a parallel test teammate.

## Prerequisites

- `claude`, `tmux`, `mcp-proxy`, `uv`, `git`, `python3` on `PATH`.
- File-based Claude credentials at `~/.claude/.credentials.json` (Linux).
  The run copies them (mode 0600) into the throwaway config dir and the
  teardown deletes the copy.

## Running

```sh
cd spikes/vox-73y7-hook-context
direnv exec ../../ uv run dry_run.py       # offline rehearsal, must PASS first
direnv exec ../../ uv run run_capture.py   # ONE bounded ~20-min capture run
direnv exec ../../ uv run teardown.py      # manual cleanup, idempotent
```

Analyzers re-run standalone over any ledger:

```sh
direnv exec ../../ uv run field_inventory.py --ledger results/run_<ts>/hook_ledger.jsonl
direnv exec ../../ uv run latency.py --ledger ... --out latency.json
direnv exec ../../ uv run gap_check.py --ledger ...
direnv exec ../../ uv run reconstructor.py --ledger ... --cutoff <cutoff_index>
direnv exec ../../ uv run seed_builder.py --ledger ... --cutoff <cutoff_index>
```

`--cutoff` is a file-order record count (the `cutoff_index` recorded in
`timepoints.json`), not a `recv_seq` — receiver sequences reset across
store restarts, so they cannot bound a timepoint.

## Isolation and bounds

- One fork per capture run (hard cap 2 = one retry), in a fresh
  `git init` project under the repo root's gitignored
  `.tmp/vox73y7-scratch/` (outside the spike tree: the fork's config dir
  pulls vendored plugin markdown that would otherwise fail `make docs`
  while a run is live), with a fresh `CLAUDE_CONFIG_DIR`; the fork env
  blanks `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`.
- The `context-capture-v1` profile allows Bash (the debug loop needs a
  test runner), unlike juhw's voice-launch profile: this fork stands in
  for the USER'S OWN working session, not a voice-launched capability
  grant. Network tools (`WebFetch`/`WebSearch`) and `Task` stay denied,
  and the prompt pins the fork to its directory with an explicit stop.
- The EL retest (inherited from vox-bst7) is capped at 3 billed sessions
  and uses the bst7 harness on main, unmodified, from its own directory;
  only artifact copies land here.
- Ledger payloads pass recursive credential-key redaction and host-path
  sanitization before persisting; committed captures are scrubbed the
  same way.
