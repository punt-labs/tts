# vox-juhw — Mode B same-host session-launch spike

Validates DES-071 Mode B v1 (DES-068 umbrella): a voxd-side
`launch_session` capability that forks a fresh `claude` session same-host,
in a detached tmux session, with a curated permissions profile, an initial
prompt derived from a voice conversation, and the spawned session's hooks
routed back over loopback to a voxd context store — **with no custom
Claude Code changes**.

Throwaway spike code: nothing here ships, nothing touches `src/`. Scripts
are self-contained PEP 723 uv scripts; run them **from this directory** so
sibling imports resolve.

## The chain under test

```text
StoreProcess (stub voxd)     SessionLauncher                claude fork
ws://127.0.0.1:<port>  <---  tmux new-session -d      --->  scratch project
        ^                    -e CLAUDE_CONFIG_DIR=...       .claude/settings.json
        |                                                     permissions profile
        +--- mcp-proxy ws://127.0.0.1:<port> --hook <Event> --+ hooks block
```

- **Fork**: `tmux new-session -d` runs `claude "<task>"` in a throwaway
  `git init` project under `.tmp/`, with a fresh `CLAUDE_CONFIG_DIR` so no
  user-level plugins, hooks, MCP servers, or state leak in.
- **Configure**: the permissions profile is a project
  `.claude/settings.json` deposited before the fork — `permissions.allow`
  (file tools only), `permissions.deny` (Bash, WebFetch, WebSearch, Task),
  `permissions.defaultMode: acceptEdits` — plus a `hooks` block routing
  SessionStart / UserPromptSubmit / PostToolUse / Stop / SessionEnd through
  the **real `mcp-proxy --hook`** binary to the stub store.
- **Attach**: verified non-interactively via `tmux capture-pane`.
- **Hook loopback**: the stub store speaks mcp-proxy's actual wire contract
  (JSON-RPC 2.0 over WebSocket, method `hook/<Event>`, params = payload),
  stamps every payload with a global and per-session monotonic sequence,
  and appends to a fsync'd JSONL ledger.

## Files

| File | Role |
|------|------|
| `stamp.py` | `SequenceStamper` (monotonic stamps + session attribution + credential redaction), `HookRecord`, `HookLedger` (durable JSONL) |
| `hook_store.py` | Stub voxd context store: WebSocket JSON-RPC server for `hook/<Event>` and `store/health` |
| `profiles.py` | `PermissionsProfile` (the `voice-launch-v1` profile), `HookWiring` (mcp-proxy relay commands), `SettingsDocument` |
| `transcript.py` | Canned Mode B voice conversation + `TaskSeed` (bounded initial prompt derivation) |
| `scratch.py` | `ScratchProject` (throwaway git-init project), `IsolatedConfig` (fresh `CLAUDE_CONFIG_DIR` with seeded credentials/state) |
| `launcher.py` | `TmuxSession`, `LaunchCommand`, `SessionLauncher` (fork cap `MAX_FORKS_PER_RUN`) |
| `teardown.py` | Idempotent teardown: kill `voxjuhw*` tmux sessions, remove `.tmp/` |
| `run_validation.py` | End-to-end run producing the four evidence items + `verdict.json` |
| `test_*.py` | Offline tests for the verdict-bearing logic — no claude session is spawned |
| `results/` | Committed evidence per run (`run_<ts>/`) |
| `REPORT.md` | Acceptance verdict + rough edges |

## Prerequisites

- `claude`, `tmux`, `mcp-proxy`, `uv`, `git` on `PATH`.
- File-based Claude credentials at `~/.claude/.credentials.json` (Linux).
  The run copies them (mode 0600) into the throwaway config dir and the
  teardown deletes the copy.

## Running

```sh
cd spikes/vox-juhw-mode-b-launch
direnv exec ../../ uv run pytest .            # offline tests, no forks
direnv exec ../../ uv run run_validation.py   # ONE bounded end-to-end run
direnv exec ../../ uv run teardown.py         # manual cleanup, idempotent
```

One validation run forks exactly one claude session (hard cap: 2), gives
it a ~600-char prompt that ends in an explicit stop instruction, drives
one extra post-kill turn, then tears everything down. Evidence lands in
`results/run_<ts>/`:

- `hook_ledger.jsonl` — stamped, attributed hook payloads (evidence 1)
- `capture_mid_run.txt` — pane mid-run (evidence 2)
- `capture_after_store_kill.txt`, `capture_post_kill_turn.txt`,
  `survival.log` — store-SIGKILL survival (evidence 3)
- `teardown.log` — two teardown passes (evidence 4)
- `verdict.json` — per-criterion + overall PASS/FAIL

## Isolation

Spawned sessions never touch a real checkout: the project is a fresh
`git init` under this directory's gitignored `.tmp/`, and the fresh
`CLAUDE_CONFIG_DIR` means no user plugins (vox, ethos, biff) run in the
fork. The fork's env also blanks `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`
so the launcher's own credentials cannot leak in. Residual leak: Claude
Code's ancestor CLAUDE.md walk still reads the enclosing worktree's docs
as context (see REPORT.md, rough edges).
