# Agent-Driven Auto-Vibe — Design

**Bead:** vox-ek1m · **Status:** IMPLEMENTED · **Supersedes:** `docs/vibe-exit-code-design.md` (deleted)

## Problem

`/vibe auto` is supposed to keep the session's TTS mood current without the user
having to set it by hand. Two prior mechanisms failed:

1. **Output-pattern classification** (DES-018 era) grepped command *output* for
   pytest/ruff/git tokens. Narrow (only this repo's toolchain), asymmetric
   (a clean exit produced no signal, so the mood skewed frustrated everywhere
   else), fragile (format drift broke it). `vox-p0u6` was the acute symptom.

2. **Exit-code accumulation** (the `feat/vibe-exit-code` branch) tried to derive
   the mood from each Bash command's exit code, read from the `PostToolUse`
   hook. **The signal does not exist.** Claude Code does not expose the Bash
   exit code to `PostToolUse` hooks.

### Why exit codes are unavailable to PostToolUse hooks

Confirmed two ways:

- **Docs.** `BashTool` interprets return codes internally and renders them into
  a human message; the hook's `tool_response` object carries only `stdout`,
  `stderr`, `interrupted`, `isImage`, and `noOutputExpected` — no exit code in
  any casing. The tool-orchestration docs further state the final tool result is
  constructed *after* `PostToolUse` hooks run, so the hook structurally cannot
  observe a finalized exit code.
- **Live capture.** A payload capture this session showed exactly those fields
  and no exit code under any key (`exitCode`, `exit_code`, `code`, `status`).

The interim exit-code accumulator read `tool_response["exitCode"]` from the
`PostToolUse` payload; that key is never present, so the parsed exit code was
always absent, the post-bash handler returned early on every event, and the
outcome window recorded nothing. The exit-code machinery was dead on arrival
and could not be fixed — the data is not in the hook. (All of it is deleted in
this change.)

## Decision

**Drive the vibe from the main agent, prompted by a non-blocking reminder.**

A `UserPromptSubmit` hook injects a soft `additionalContext` reminder — "glance
at how the session is going; set the vibe if the mood shifted." The main agent,
which sees the whole conversation (the real success/failure signal a
per-command payload lacks), sets the vibe via the existing `vibe` MCP tool. No
deterministic classification anywhere.

Validated by a live spike this session: a `UserPromptSubmit` hook emitting
`hookSpecificOutput.additionalContext` fired live, delivered the reminder, and
the agent set an accurate vibe end-to-end. The docs basis:
`07-instructions-memory` names `UserPromptSubmit` as the mechanism for
"prompt-time dynamic context" and notes hooks are more reliable than a static
CLAUDE.md instruction.

Rationale (operator, 2026-07-12): the agent already holds the context that makes
a good mood judgment; the per-command hook never did.

## Mechanism

```text
user submits prompt
        │
        ▼
UserPromptSubmit hook (hooks/vibe-nudge.sh → vox hook vibe-nudge)   [synchronous, non-blocking]
        │
        ├─ vibe_mode != auto ──────────────────▶ emit nothing, exit 0
        │
        └─ vibe_mode == auto
                │  read vibe_nudge_turns from vox.local.md
                │  turns += 1
                ├─ turns < N ──────────────────▶ persist turns, emit nothing
                └─ turns >= N ─────────────────▶ persist turns=0, emit reminder
                                                         │
                                                         ▼
                          {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                   "additionalContext": <reminder>}}
                                                         │
                                                         ▼
                              main agent reads it, optionally calls vibe(mood=…, tags=…)
```

Four properties are load-bearing:

- **Non-blocking, always.** The hook emits `additionalContext` or nothing. It
  never emits `{"decision": "block"}`. Blocking is the Stop/summary hook's job
  (a separate mechanism, untouched here).
- **Synchronous, not async.** Only synchronous `UserPromptSubmit` output is
  injected into the model's context. The reminder hook must not be `async` (the
  existing async `acknowledge.sh` speech hook stays async and separate).
- **Gated on `auto`.** In `manual` the user owns the vibe; in `off` there is no
  vibe. The hook reads `vibe_mode` from config and does nothing outside `auto`.
- **Cadence-gated.** It fires every `N`th prompt, not every turn.

### Cadence and where its state lives

Throttle: **every `N`th user prompt**, `N = 5` by default (tunable). A counter
`vibe_nudge_turns` lives in the **ephemeral** `vox.local.md` config file,
alongside the rest of the vibe cluster.

Why there: each hook invocation is a fresh subprocess, so an in-process counter
cannot survive across prompts. `vox.local.md` is the existing per-session
ephemeral store (it already held `vibe_mode`/`vibe`/`vibe_tags`), and the
counter is exactly one integer key co-located with the mode that gates it. One
small ephemeral field, one file write per auto-mode prompt (<10 ms, off the
critical path since the hook is non-blocking).

Rule: on each auto-mode prompt, `turns += 1`; when `turns >= N`, emit the
reminder and reset `turns = 0`. No nudge on prompts `1..N-1`. The first nudge of
a session lands on prompt `N`, giving the session enough activity to be worth
judging.

Coherence with `/vibe`: setting any mode via the `vibe` tool resets
`vibe_nudge_turns` to `0` (`VibeChange.resolve`), so re-entering `auto` starts a
clean `N`-prompt runway. `SessionEnd` also resets it, so the counter never leaks
across sessions.

This is the whole state: a single bounded integer. There is no window, no mood
accumulator, no state machine.

### Where the reminder prose lives

`vibe_nudge.py` — the `VIBE_NUDGE_REMINDER` constant lives beside `VibeNudge`,
the class that consumes it. It is a single deterministic string (not a random
pool: the user never sees it, the agent reads it, and a fixed string is
testable), and it is silent injected context, not spoken audio — so it is not
a speech quip.

## No formal model

**No Z model is written for this change, by design.** The state machine that
justified the earlier Z model (`docs/vibe-exit-code.tex` — the exit-code
window/mood accumulator with its `focus_from`/`weary_from`/`max_window`
invariants) is being removed. The replacement is a stateless nudge plus a
trivial bounded cadence counter (`turns` in `[0, N)`), which is below the
formal-modeling trigger: no 3+ mode transitions, no cross-transition invariant
whose silent violation corrupts state. Modeling a mod-`N` counter would be
ceremony without payoff.

## No migration or compatibility code

**No migration, compat, shim, or version-detection code — by design (PL-PP-1,
PY-RF-6).** Punt Labs products have no installed base to migrate. The exit-code
machinery is deleted outright in the same change (forward integration); the
`vibe_signals` config field it wrote is removed rather than bridged. No
`vibe_signals`→`vibe_nudge_turns` translator, no legacy-token reader, no
re-export tombstone. A stale `vibe_signals` line left in someone's
`vox.local.md` is simply ignored (the parser only reads keys it knows).

## What gets deleted

Deletion is a first-class deliverable, weighted equally with the additions. The
full dead-code set — every dead file and every dead function — is enumerated in
the implementation write-set (`.tmp/missions/results/vox-ek1m-design.md`,
"Delete-set"). In summary, the whole exit-code path goes:

- The branch-added accumulator modules `vibe_window.py` and `vibe_mood.py` (and
  their tests), and the Z model `docs/vibe-exit-code.tex`.
- `BashPayload` and its exit-code parsing (`hook_payload.py`) — its only
  consumer was `handle_post_bash`.
- `handle_post_bash` / `post_bash_cmd` and the `PostToolUse` Bash hook
  (`hooks/signal.sh` + its `hooks.json` entry).
- `_persist_auto_vibe_tags` (resolved dead exit-code signals to tags).
- The `vibe_signals` config field everywhere it is plumbed — `config.py`,
  `server.py`, `vibe.py`, `__main__.py` status, and the Stop-hook gate.
- `docs/vibe-exit-code-design.md`, which this document replaces.

The already-landed branch deletions (the old output-pattern classifier
`command_signal.py`, the `watcher.py` milestone announcer, `chime.py`/`mood.py`,
`signal.py`, and the milestone chime assets) stay deleted — verified: no live
references remain.

The Stop/summary decision-block mechanism is **preserved**. Only its dead
dependency on `vibe_signals` is removed; see the write-set for the resulting
gate.

## What stays

- The `vibe` MCP tool and `/vibe` command — the agent's entry point, unchanged
  in surface. `/vibe auto` re-enables the nudge; `/vibe manual` and `/vibe off`
  silence it (the hook gates on `vibe_mode`).
- The continuous-mode `UserPromptSubmit` speech acknowledgment
  (`acknowledge.sh` → `handle_user_prompt_submit`, gated on `notify == "c"`).
  It is a separate, async hook on the same event and is orthogonal to the vibe.
- Notification chimes, the Stop summary, `vibe_tags` applied to synthesized
  speech — all untouched.

## Rejected alternatives

| Alternative | Rejected because |
|---|---|
| Exit-code per command (the branch) | The signal does not exist — `PostToolUse` hooks never see the Bash exit code (see Problem). |
| Output-pattern classification (DES-018) | Narrow, asymmetric, fragile. |
| LLM call inside the hook | The hook runs outside the model's context and must be near-instant; an in-hook LLM call is slow, costly, and blind to the conversation the reminder exists to leverage. |
| Nudge every prompt | Nags. The cadence counter throttles to every `N`th prompt. |
| Counter in memory / in the daemon | A fresh subprocess per hook can't hold in-process state; the daemon has no session/vibe awareness by design (it owns audio only). The ephemeral config file is the session store. |
| Reset the nudge counter via a migration from `vibe_signals` | No migration code (PL-PP-1); `vibe_signals` is deleted, not translated. |
