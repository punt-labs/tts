# Spike vox-04qy — Steering a Running Mode B Session

**Bead:** vox-04qy (P1). **Status:** PLAN — not yet dispatched.
**Question:** can an external process deliver a mid-run user turn into a
launched Mode B session, with confirmed receipt and characterized mid-turn
semantics? Two candidate channels, one arm each.

DES-068 Wall 2 says "no background process can inject" — this spike gathers
the evidence for (or against) amending that. The amendment itself is an
operator decision recorded in DESIGN.md after the spike, before any
implementation dispatches.

## Arm 1 — pi RPC `steer` verb

DES-066's spike proved pi's RPC protocol live (`prompt` → event stream,
persistent process, warm cache) but never sent a `steer`, and its harness
was `.tmp` scratch, now gone. This arm rebuilds the harness — committed —
and exercises `steer` specifically.

- Spawn `pi --mode rpc --no-session --no-extensions` over direct
  `subprocess.Popen` pipes (DES-066's own recommendation over tmux for a
  daemon-held process; the tmux/`keep` leg stays out of scope here).
- Send a `prompt` engineered to produce a long multi-step turn; send
  `steer` mid-turn. Capture the full JSONL event stream.
- Measure/characterize: does `steer` interrupt or queue; what happens to
  the in-flight tool call (the bst7 barge-in question, now on the pi side);
  stream coherence after; latency from steer-write to first steered output
  event. Also exercise `steer` at idle and contrast with `follow_up`.

**Precondition (resolved 2026-08-31):** operator installed `pi` 0.84.4 on
pembroke via the official installer (`pi.dev/install.sh`); `--mode rpc` and
the `--tools` read-only restriction are confirmed present in `--help`.
`opencode` 1.18.25 and `codex` 0.151.0 were installed alongside it.

## Arm 2 — claude TUI in tmux via `send-keys`

The Pi-tool pattern: type into the live Claude Code TTY. Claude Code has no
injection API (Wall 2), but the TTY is the interactive loop itself.

- Reuse (copy, never edit) the frozen juhw machinery: `launcher.py` fork
  into a detached tmux session, `profiles.py` deposited settings profile,
  `scratch.py` isolation, `teardown.py` verified kill — plus the 73y7
  `wiring.py`/`hook_store.py` relay so the spawned session's hooks land in
  a local store.
- Test matrix:
  1. **Idle inject** — send-keys a prompt at turn boundary; Enter submits.
  2. **Mid-turn inject** — send while the agent is working; characterize
     whether Claude Code queues it as a pending user message or drops it.
  3. **Interrupt-then-steer** — Esc (or the documented interrupt key) then
     replacement text; the "hard steer".
  4. Mechanics: `-l` literal mode, bracketed paste, Enter timing after
     paste settles, multi-line text, TUI redraw races.
- **Receipt is the hook store**: our injected text appearing as a
  `UserPromptSubmit` event (sender-stamped, per DES-070's validated relay)
  is the delivery confirmation; tmux `capture-pane` is the secondary
  witness. Measure send→hook-visible latency.

## Pass / fail

- **Pass (per arm):** a steering input delivered mid-turn reaches the agent
  with confirmed receipt, and the mid-turn semantics (interrupt vs queue vs
  drop; in-flight tool-call fate) are characterized with committed
  evidence.
- **Fail (per arm):** input is dropped or mangled mid-turn with no workable
  mitigation. A characterized limitation with a mitigation (e.g. "mid-turn
  send-keys queues rather than interrupts; hard steer requires Esc first")
  is a PASS with documented semantics, not a fail.

## Discipline carried forward

- Committed harness under `spikes/vox-04qy-steering/` — PEP 723 uv scripts,
  own `ruff.toml`, colocated tests, nothing under `src/`. The DES-066
  harness was scratch and is gone; that mistake is not repeated.
- h7k8 isolation rules: sentinel stubs first on PATH, no real vox-panel /
  live Lux hub reachable, cwd at an unenabled scratch dir outside the repo,
  process-group teardown with pgrep-before-pkill evidence preservation.
- Zero ElevenLabs spend — no EL session is involved in either arm.
- Claude fork budget: bounded, scratch-isolated, torn down (juhw pattern).

## Mission sketch (dispatch when the batch starts)

- One spike mission, `quick` pipeline. Worker `rmh`, evaluator `gvr` (same
  pairing as juhw/73y7 — Python harness engineering).
- Contract carries: the two arms above, the pass/fail bar, commit-per-step
  with `make check` green, the isolation rules by reference to the h7k8
  guards, and the requirement that findings land in a REPORT.md with the
  raw event streams / pane captures committed sanitized.
- Leader (me) runs local review pair on the diff, then the PR loop.

## Outputs

1. `spikes/vox-04qy-steering/REPORT.md` — per-arm verdicts + semantics.
2. Committed harnesses + event-stream/pane-capture evidence.
3. A drafted DES-073 (or amendment section under DES-068) presenting the
   Wall-2 evidence and the recommended steering channel per agent type —
   to the operator for ratification BEFORE implementation dispatches.

## Open questions for the operator (at dispatch, not now)

1. Whether steering lands in E+ v1 scope (`launch_session` + steer) or is
   validated now and shipped later.
2. The operator installed `codex` alongside pi and opencode — a signal
   that DES-071's `launch_session` agent set (`claude | pi | opencode`)
   may want `codex` added. If so: does the spike grow a codex steering
   arm (codex also runs as a TUI, so it likely shares Arm 2's tmux
   send-keys channel), or is codex support a separate follow-on bead?
