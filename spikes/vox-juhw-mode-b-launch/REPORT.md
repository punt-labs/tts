# vox-juhw REPORT — Mode B same-host session launch (DES-071 v1)

**Verdict: fork → configure → attach → hook-loopback works end-to-end
without custom Claude Code changes: PASS.**

Adjudicated run: `results/run_20260830_171420/` (`verdict.json` —
overall PASS, zero interactive nudges required). One claude session
forked, bounded, and torn down; 53 offline tests cover the
verdict-bearing logic without spawning any session.

## What was validated, mechanism by mechanism

### (a) Fork: detached tmux, specified permissions profile

`tmux new-session -d -s voxjuhw-<ts> -c <scratch-project>
-e CLAUDE_CONFIG_DIR=<fresh-dir> -e ANTHROPIC_API_KEY= ... claude '<task>'`
(`launcher.py`). **The permissions-profile mechanism is a project
`.claude/settings.json` deposited by the launcher before the fork** —
not a CLI flag. It carries the `voice-launch-v1` profile
(`profiles.py`): `permissions.allow = Read/Write/Edit/Glob/Grep/TodoWrite`,
`permissions.deny = Bash/WebFetch/WebSearch/Task/NotebookEdit`,
`permissions.defaultMode = acceptEdits`. Claude Code honored it with no
flags: the fork's startup banner lists the six pre-approved tools, edits
auto-accepted inside the project, and the session never asked for Bash.

### (b) Initial prompt derived from a canned voice transcript

`transcript.py` fixes a five-turn Mode B conversation and derives a
600-char task prompt ending in an explicit stop instruction. The fork
received it as `claude`'s positional argument, created `greeting.py` +
`README.md` in the scratch project, replied DONE, and idled
(`capture_mid_run.txt` shows the prompt echoed and `Write(greeting.py)`
in flight).

### (c) Hooks over loopback to a stub voxd context store

**The real `mcp-proxy --hook` was used — no stand-in.** The deposited
hooks block routes SessionStart / UserPromptSubmit / PostToolUse / Stop /
SessionEnd through `mcp-proxy ws://127.0.0.1:<port> --hook <Event>` to
`hook_store.py`, a WebSocket JSON-RPC server speaking mcp-proxy's exact
contract (method `hook/<Event>`, params = payload, sync response by id).
Each payload is stamped with a global `recv_seq`, a per-session
`session_seq`, and the payload's `session_id`, then fsync-appended to
JSONL **before** the acknowledgement is sent.

## Evidence per acceptance criterion

1. **Hooks land, ordered, session identifiable** —
   `results/run_20260830_171420/hook_ledger.jsonl`: 5 records
   (SessionStart, UserPromptSubmit, PostToolUse ×2, Stop), `recv_seq`
   1–5 strictly increasing, `session_seq` 1–5, every record attributed
   to the fork's real session id `de9dd114-6fd…`. Ordering,
   attribution, and redaction invariants are also pinned by
   `test_stamp.py` and `test_hook_store.py`.
2. **Attach shows a usable session mid-run** —
   `results/run_20260830_171420/capture_mid_run.txt` (non-interactive
   `tmux capture-pane`): Claude Code banner, the derived prompt, and the
   first Write tool call, live in the scratch project.
3. **Killing the stub voxd does not kill/orphan the fork** —
   `results/run_20260830_171420/survival.log`: the store's **process
   group** SIGKILLed (`os.killpg` — killing only the `uv run` wrapper pid
   leaves the python store alive; an earlier run demonstrated exactly
   that, so the honest kill is load-bearing); the port then refuses
   connections; the tmux session stays alive; the fork answers a
   follow-up turn ("ALIVE" — `capture_post_kill_turn.txt`); the ledger
   does not grow (5 → 5). The pane shows the designed failure mode:
   `Stop hook error: Failed with non-blocking status code: mcp-proxy:
   connection refused` — the relay fails harmlessly, the session
   continues.
4. **Teardown exists, clean, idempotent** —
   `results/run_20260830_171420/teardown.log`: first pass kills the
   tmux session and removes the scratch root (including the credentials
   copy); second pass finds nothing and still exits clean.
   `test_teardown.py` pins idempotence offline.

## Isolation (mandatory) — how it was held

- Fork works in a fresh `git init` project under the spike's gitignored
  `.tmp/`, never a real checkout.
- Fresh `CLAUDE_CONFIG_DIR` per fork: no user plugins (vox, ethos,
  biff), hooks, or MCP servers exist for the spawned session. Seeded
  into it: a copy of `~/.claude/.credentials.json` (mode 0600, deleted
  by teardown) and a minimal `.claude.json` (`hasCompletedOnboarding`,
  `hasTrustDialogAccepted` for exactly the scratch path,
  `hasClaudeMdExternalIncludesApproved: false`).
- Fork env blanks `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`.
- Bounded: 600-char prompt with explicit stop; one fork per run
  (hard cap 2, enforced and tested); `Bash` denied by profile; ledger
  payloads pass a credential-shaped-key redaction before persisting.

## ROUGH EDGES

Per the bead: any edge requiring custom Claude Code work turns Mode B v1
into a quarter. **None of the edges below requires Claude Code changes**
— they are launcher-side (voxd) engineering, config seeding, or accepted
v1 scope. Itemized honestly:

1. **Trust-dialog pre-seeding is path-exact and settings-sensitive.**
   `.claude.json` `projects.<path>.hasTrustDialogAccepted: true` works
   only when the key is the exact absolute project path; with a relative
   path the fork stalls on a trust dialog whose stronger variant (shown
   when settings.json pre-approves tools) **defaults to "No, exit"**.
   The harness keeps a keystroke-nudge fallback (Down+Enter) but the
   adjudicated run needed zero nudges. voxd's launcher must write the
   resolved path. Launcher-side only.
2. **Credential seeding is undocumented surface.** The fork
   authenticates via a copied `~/.claude/.credentials.json` into the
   fresh `CLAUDE_CONFIG_DIR`; macOS keychain-backed installs would
   need a different seed path. It's private Claude Code state, so its
   layout can shift under auto-update. Risk, not a blocker.
3. **Env hygiene is the launcher's job.** tmux sessions inherit the
   server environment: the first run tripped a "Detected a custom API
   key — use it?" dialog from the workspace's `ANTHROPIC_API_KEY`.
   Fixed by blanking the variables in the fork env. A voxd port must
   launch with a curated environment, not its own.
4. **Kill semantics: process *group*, not pid.** `uv run` wraps the
   store; killing the wrapper orphans the child (observed: the "dead"
   store kept accepting hooks). Any voxd supervision of launched
   processes needs process-group discipline. Also: the store's
   per-connection handler sees `ConnectionClosedError` when a relay
   drops without a close frame — harmless here (the server keeps
   serving), but a real voxd port should catch it per connection.
5. **Ancestor CLAUDE.md leak (accepted v1 scope).** With the scratch
   project under the vox worktree, the fork's context walk still reads
   the enclosing workspace docs (visible in session context, though the
   external `@`-imports stay unapproved). Real voxd launches would use
   `~/.punt-labs/voxd/scratch/<id>` or the user's chosen project dir,
   outside any workspace — placement, not code.
6. **No machine-readable readiness signal.** The launcher learns the
   fork is up only from the SessionStart hook arriving (which doubles
   as the readiness probe here — fine for v1, worth noting for the
   `launch_session` tool's return contract).
7. **Login expiry is a live dependency** — the fork's banner warned
   "login expires in 2 days"; an expired credential file would stall a
   voice-initiated launch on an interactive `/login`. voxd should
   surface credential health before offering `launch_session`.

## Costs and bounds

Two end-to-end runs were executed in total during development (the first
invalidated and its evidence deleted when the wrapper-pid kill was
caught); each forked exactly one claude session for roughly two minutes
of wall clock and two short turns of Opus usage. No ElevenLabs credits
were involved anywhere in this spike.
