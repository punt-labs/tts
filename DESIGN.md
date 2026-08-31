# punt-vox Design Decision Log

This file is the authoritative record of design decisions, prior approaches, and their outcomes. **Every design change must be logged here before implementation.**

## Rules

1. Before proposing ANY design change, consult this log for prior decisions on the same topic.
2. Do not revisit a settled decision without new evidence.
3. Log the decision, alternatives considered, and outcome.

---

## System Architecture

```text
┌──────────────────────────────────────────────────────────────┐
│                        Claude Code UI                        │
│                                                              │
│  ┌──────────────┐  ┌──────────────────┐  ┌───────────────┐  │
│  │  Tool Result  │  │ Assistant Output │  │  Slash Cmds   │  │
│  │    Panel      │  │   (LLM emits)    │  │  /notify etc  │  │
│  └──────┬───────┘  └────────▲─────────┘  └───────┬───────┘  │
│         │                   │                     │          │
└─────────┼───────────────────┼─────────────────────┼──────────┘
          │ updatedMCP        │ model output         │ skill
          │ ToolOutput        │                     │ prompt
          │                   │                     │
┌─────────┴───────────────────┴─────────────────────┴──────────┐
│                        Hook Layer                             │
│                                                              │
│  Stop hook (notify.sh):                                      │
│    if notify=y and not stop_hook_active:                     │
│      decision=block, reason="summarize + call TTS"           │
│    → Claude generates 1-2 sentence summary + calls speak     │
│    → stop_hook_active=true on second fire → let stop         │
│                                                              │
│  Notification hook (notify-permission.sh):                   │
│    if notify=y: async call `vox say` CLI directly            │
│    → audio plays immediately, no model involvement           │
│                                                              │
│  PostToolUse hook (suppress-output.sh):                      │
│    formats TTS MCP tool output for UI panel                  │
│                                                              │
└──────────────────────────────────────────────────────────────┘
          │                                    │
          │ MCP tool calls                     │ CLI calls
          │                                    │
┌─────────▼────────────────────────────────────▼───────────────┐
│                    punt-vox Engine                            │
│                                                              │
│  vox mcp (stdio, thin client) ──► voxd :8421/ws (WebSocket) │
│  vox hook <event> (Python)   ──► voxd :8421/ws (WebSocket) │
│  vox say (CLI)               ──► voxd :8421/ws (WebSocket) │
│                                                              │
│  voxd: synthesis, playback queue, dedup, cache (DES-028)    │
│  Providers: ElevenLabs > OpenAI > Polly > say > espeak      │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

## State Management

Per-project config lives in `.punt-labs/vox/` as two files:

```text
.punt-labs/vox/vox.md          # tracked in git — durable preferences
---
voice: ""
provider: ""
model: ""
notify: "n"
speak: "y"
---

.punt-labs/vox/vox.local.md    # gitignored — ephemeral session state
---
vibe: ""
vibe_mode: "auto"
vibe_tags: ""
vibe_nudge_turns: "0"
---
```

Durable keys (voice, provider, model, notify, speak) route to `vox.md`. Ephemeral keys (vibe, vibe_mode, vibe_tags, vibe_nudge_turns) route to `vox.local.md`. All hooks and commands read these files for current state. See DES-012 for why this is per-project, not global, and DES-036 for the two-file split.

---

## DES-001: Notification Architecture — Stop Hook with Decision Block

**Date:** 2026-02-25
**Status:** SETTLED
**Topic:** How task-completion notifications work

### Design

The Stop hook uses `decision: "block"` to make Claude generate one more turn with a spoken summary. This is the only mechanism available — the Stop hook does not support `additionalContext`.

**Flow:**

```text
Claude finishes → Stop hook fires → reads .punt-labs/vox/vox.local.md
  ├── notify=n → exit 0 (let stop, no notification)
  ├── stop_hook_active=true → exit 0 (prevent infinite loop)
  └── notify=y|c → return { decision: "block", reason: "..." }
        → Claude generates 1-2 sentence summary
        → Claude calls TTS speak tool (ephemeral, auto_play)
        → Claude stops → Stop hook fires again
        → stop_hook_active=true → exit 0 (done)
```

**The `reason` field is the prompt.** It tells Claude:

- Summarize what you just did in 1-2 sentences
- Call the TTS speak tool with ephemeral=true, auto_play=true
- Do not add any other commentary

### Why This Design

- The model understands what it just did and can generate intelligent summaries
- `last_assistant_message` is available but would require external summarization (API call) or dumb truncation if processed in shell only
- `stop_hook_active` provides a built-in infinite loop guard
- The extra model turn is minimal (1-2 sentences + tool call)

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|-----------------|
| Shell-only: extract + truncate `last_assistant_message`, call `vox say` CLI | No intelligent summarization; shell truncation produces poor summaries |
| Async hook with CLI call | Cannot block the stop to get a summary; would only produce "task complete" with no context |
| `additionalContext` in Stop hook | Not supported — Stop hook only has `decision`/`reason` for control |

### UX Concern: Extra Model Turn

The user sees Claude generate one more message (the summary). This is acceptable because:

1. The summary is brief (1-2 sentences)
2. The audio plays while the user reads, not instead of reading
3. The skill prompt instructs minimal output

If this proves annoying, the fallback is Approach B: async shell-only with `vox say "Task complete"` (no summary, just a notification).

---

## DES-002: Permission-Prompt Notification — Async CLI Call

**Date:** 2026-02-25
**Status:** SETTLED
**Topic:** How permission-prompt notifications work

### Design

The Notification hook (matcher: `permission_prompt`) fires an async shell command that calls the `tts` CLI directly. No model involvement.

**Flow:**

```text
Permission dialog appears → Notification hook fires (async)
  → reads tts.local.md
  ├── notify=n → exit 0
  ├── speak=n → play chime audio file
  └── speak=y → pick random phrase → vox synthesize "$TEXT" -o $TMPDIR/notify.mp3
```

### Why Async + CLI (Not Model)

- The notification message is already clear ("Claude needs permission to use Bash")
- No summarization needed — just announce it
- Async avoids blocking the permission dialog
- CLI call is fast and self-contained

### Why Not MCP Tool

The Notification hook runs outside the model's conversation. It cannot call MCP tools (those require the model to invoke them). The CLI is the correct interface for hook-initiated synthesis.

---

## DES-003: State File — Extended Config

**Date:** 2026-02-25
**Status:** SUPERSEDED by DES-012, then DES-036
**Topic:** Where notification and speech state is persisted

### Original Design (Superseded)

Originally used `~/.claude/tts.local.md` (global). Moved to `.vox/config.md` (per-project) in DES-012. Then split into `.punt-labs/vox/vox.md` + `vox.local.md` in DES-036 (v4.7.5). See the State Management section at the top for current layout.

---

## DES-004: /speak Toggle — Voice vs. Chime

**Date:** 2026-02-25
**Status:** SETTLED
**Topic:** How `/speak y` and `/speak n` control notification audio

### Design

`/speak y` = spoken words via TTS provider. `/speak n` = short audio tone (chime).

Chime audio is a pre-generated MP3 file bundled in the package. Two distinct tones:

- `chime_done.mp3` — task completed (pleasant, resolving)
- `chime_prompt.mp3` — needs approval (attention-getting, rising)

Played via `afplay` (macOS) directly from the hook script.

### Why Pre-Generated

- No API call needed for chimes — instant playback
- No provider dependency for chime-only mode
- Consistent sound regardless of provider availability

---

## DES-005: /recap — On-Demand Spoken Summary

**Date:** 2026-02-25
**Status:** SETTLED
**Topic:** How `/recap` generates and speaks a summary

### Design

`/recap` is a slash command (skill prompt) that instructs the model to:

1. Summarize the key points of its last response in 2-3 sentences
2. Call the TTS speak tool with the summary (ephemeral, auto_play)
3. Show the summary text in the conversation

### Why Skill Prompt (Not Hook)

- `/recap` is user-initiated, not event-driven
- The model needs to read its own context to summarize
- A skill prompt is the natural interface for "do something and speak it"

---

## DES-006: Plugin Hook Registration

**Date:** 2026-02-25
**Status:** SETTLED
**Topic:** How notification hooks are registered in the plugin

### Design

Hooks are declared in `plugin/hooks/hooks.json` and registered by the plugin system. Each Claude Code event maps to a focused script under `plugin/hooks/` (current registration):

| Event | Script(s) | Notes |
|-------|-----------|-------|
| `SessionStart` | `session-start.sh` | Deploys commands, cleans retired ones, auto-allows `mic` tools |
| `PostToolUse` | `suppress-output.sh` | Matcher `mcp__…mic__.*` — formats MCP tool output for the panel (DES-008) |
| `Stop` | `notify.sh` | Task-completion notification (DES-001) |
| `PreCompact` | `pre-compact.sh` | async |
| `Notification` | `notify-permission.sh` | Matchers `permission_prompt`, `idle_prompt`; async (DES-002) |
| `UserPromptSubmit` | `acknowledge.sh` (async), `vibe-nudge.sh` | Vibe nudge fires only when `vibe_mode == auto` (DES-043) |
| `SubagentStart` / `SubagentStop` | `subagent.sh` | async |
| `SessionEnd` | `farewell.sh` | async |

Each entry is a `{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/hooks/<script>"}` object under its event key, some carrying a `matcher` and/or `"async": true` as shown.

### Why Separate Scripts

- Stop hook (synchronous, returns JSON decision) has different logic than Notification hook (async, calls CLI)
- Separate scripts keep each handler focused and testable
- The permission and idle hooks share the same script (both announce a message)

---

## DES-007: MCP Tool Naming — Voice Domain Vocabulary

**Date:** 2026-02-25
**Status:** SUPERSEDED by DES-051 (tools consolidated to `unmute` + one tool per audio group)
**Topic:** MCP tool names visible in the UI panel

> **Superseded (2026-07-29):** `chorus`/`duet`/`ensemble` no longer exist — multi-voice is now the `segments` argument of the `unmute` tool (DES-042); DES-050/DES-051 consolidated the MCP surface to `unmute` plus one subcommand-dispatched tool per audio group (`music`, `rec`); confirmed absent from `server.py`. The voice-vocabulary instinct survives in `unmute`/`mute`/`who`/`♪` (DES-042); these four names do not.

### Design

Renamed the four MCP tools from clinical/technical names to voice/audio-themed names:

| Old (clinical) | New (on-brand) | Why |
|----------------|---------------|-----|
| `synthesize` | `speak` | The natural verb for giving voice to text |
| `synthesize_batch` | `chorus` | Multiple texts at once, like a chorus |
| `synthesize_pair` | `duet` | Two texts stitched together |
| `synthesize_pair_batch` | `ensemble` | Multiple pairs, like an ensemble |

CLI command names (`tts synthesize`, `tts batch`, etc.) and internal Python API are unchanged — only the MCP tool names visible in the UI.

### Why This Matters

MCP tool names appear in the tool-result panel every time a tool is called. "synthesize" reads like a chemistry lab; "speak" reads like what the plugin actually does. Follows the dungeon plugin pattern where `load`/`save`/`delete` became `recall`/`inscribe`/`obliterate`.

---

## DES-008: Two-Channel Display — Panel + Model Context

**Date:** 2026-02-25
**Status:** SETTLED
**Topic:** How MCP tool results display in the Claude Code UI

### Design

The PostToolUse hook (`suppress-output.sh`) splits tool output into two channels:

1. **`updatedMCPToolOutput`** — Compact panel line with `♪` prefix, voice, and provider:
   - `♪ "Hello world" — matilda (elevenlabs)`
   - `♪♪ 3 tracks — matilda (elevenlabs)`
   - `♪ "Hello | Hallo" — matilda+hans (elevenlabs)`
   - `♪♪ 5 pairs — matilda (elevenlabs)`

2. **`additionalContext`** — Full JSON result for the model to reference paths, metadata, etc.

Follows the two-channel display pattern from punt-kit/patterns/two-channel-display.md.

### Why `♪`

Biff uses `▶` as its visual glyph. `♪` (musical note) is the natural symbol for a voice/audio plugin — instantly recognizable, visually distinct from other plugins.

---

## DES-009: Notification Phrase Variation

**Date:** 2026-02-25
**Status:** SETTLED
**Topic:** How permission/idle notifications avoid repetitive phrasing

### Design

The notification hook (`notify-permission.sh`) selects from a pool of 7 natural-sounding phrases per notification type using bash `$RANDOM`. Avoids the robotic repetition of hearing "Needs your approval" every time.

Phrases are stored directly in the script (no external config). Selection uses a Bash 3.2-compatible `pick_random` function that takes array elements as positional arguments (no namerefs).

---

## DES-010: Plugin Install-or-Update — Never Leave Users on Old Versions

**Date:** 2026-02-26
**Status:** SETTLED
**Topic:** How `tts install` handles already-installed plugins

### Problem

`claude plugin install tts@punt-labs` returns non-zero with "already installed" when the plugin exists. Our installer treated this as success and moved on. Users on old versions had **no update path** — they were stuck unless they manually ran `claude plugin uninstall` + `install`.

This was invisible for a long time because the developer always has the latest (editable install). It only surfaced when a second machine ran the install script after a version bump.

### Design

Install follows an install-or-update pattern:

```text
claude plugin install tts@punt-labs
  ├── exit 0              → installed (fresh)
  ├── "already installed" → claude plugin update tts@punt-labs
  │     ├── exit 0           → updated
  │     ├── "up to date"     → already up to date (success)
  │     └── other error      → fail with message
  └── other error         → fail with message
```

### Key Details

- `_install_plugin()` calls `_update_plugin()` on the "already installed" path — single responsibility per function
- `_update_plugin()` handles three outcomes: updated, already current, error
- `install.sh` does not need changes — it calls `tts install` which delegates to `installer.py`
- The `claude plugin update` subcommand was discovered empirically via `claude plugin --help`; it is not documented in public Claude Code docs as of 2026-02-26

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|-----------------|
| Always uninstall then reinstall | Destructive; removes user's plugin state; slower |
| Tell users to manually update | Poor UX; they won't know to do it |
| Check version before install | No reliable way to query installed plugin version from CLI |

---

## DES-011: install.sh Under `set -eu` — Guard Expected Failures

**Date:** 2026-02-26
**Status:** SETTLED
**Topic:** How install.sh handles commands that may fail

### Problem

`install.sh` uses `set -eu` for safety. A bare command that exits non-zero kills the entire script before any error message can print:

```bash
set -eu
"$BINARY" install       # exits non-zero → script dies silently
INSTALL_EXIT=$?          # never reached
```

This was discovered when a user ran `install.sh` from a directory with no git repo. `tts install` (which runs `claude plugin install` → git clone) failed, and the script exited silently after "Setting up Claude Code plugin..." with no error message.

### Design

Wrap expected-failure commands in `if !` guards:

```bash
if ! "$BINARY" install; then
  fail "Plugin install failed"
fi
```

The `if` construct exempts the command from `set -e` — a non-zero exit runs the else branch instead of killing the script. This is POSIX-standard behavior.

### Rule

**Any command in `install.sh` that might legitimately fail must use `if !` or `||` to handle the failure path.** Bare commands are only safe for operations that should always succeed (like `printf`).

### Context

The user ran `install.sh` from a non-git directory. The SSH fallback added an HTTPS git rewrite, but `tts install` still failed (possibly because `claude plugin install` needs a working git clone and the environment was unusual). The silent exit meant the user had to manually diagnose and run `tts install` themselves.

---

## DES-012: Per-Project Config — Not Global

**Date:** 2026-02-26
**Status:** SETTLED (path evolved: `.vox/config.md` → `.punt-labs/vox/vox.md` + `vox.local.md` in DES-036)
**Topic:** Where TTS plugin state (notify, speak, voice) is stored

### Problem

The original state file was `~/.claude/tts.local.md` — a global path shared across all Claude Code sessions in all projects. Enabling `/notify y` in one project enabled it everywhere. This is wrong: notification preferences are per-project.

### Design

State moved to per-project config in the repo root. Originally `.vox/config.md`, now `.punt-labs/vox/vox.md` (durable, tracked) + `vox.local.md` (ephemeral, gitignored). See DES-036 for the two-file split rationale.

### Why This Works

- Config directory follows the org filesystem standard (`.punt-labs/<tool>/`)
- Hooks run in the project root, so relative paths resolve correctly
- Each project gets independent `/notify`, `/speak`, `/voice` settings
- Durable prefs are tracked in git; ephemeral state is gitignored

### Migration

No migration needed from global to per-project. The `.vox/` → `.punt-labs/vox/` migration was handled by auto-migration in `vox install` and `vox daemon install` (v4.6.0).

---

## DES-013: Serialized Audio Playback via flock

**Date:** 2026-02-26
**Status:** SUPERSEDED by DES-028 (the daemon's single serialized `PlaybackQueue`)
**Topic:** How concurrent audio playback from MCP tools, Stop hook, and Notification hook is coordinated

> **Superseded (DES-028):** cross-process `flock` serialization of playback gave way to the single-daemon `PlaybackQueue` once `voxd` became the sole audio host. DES-048 later reused this ADR's size-check-then-rename `flock` *rotation pattern* for the unified `vox.log` — the pattern lives on, the playback use does not.

### Problem

Three independent audio playback paths can fire simultaneously:

1. **MCP tools** — `speak`/`chorus`/`duet`/`ensemble` in `server.py` play audio after synthesis
2. **Stop hook** — `notify.sh` plays `chime_done.mp3` on task completion
3. **Notification hook** — `notify-permission.sh` plays chime or synthesized speech

When multiple paths fire at once, audio overlaps (cacophony). PR #17 attempted PID-based kill-previous — it prevented overlap but silenced the interrupted speaker.

### Design

Every playback invocation acquires `LOCK_EX` on `~/.punt-labs/vox/playback.lock`, runs `afplay` synchronously, then releases. Concurrent callers block on the lock and play in turn.

```text
Process A: flock(LOCK_EX) → afplay file1.mp3 → release
Process B:     [blocked]  ──────────────────→ flock(LOCK_EX) → afplay file2.mp3 → release
```

Two entry points in `playback.py`:

- `play_audio(path)` — blocking: flock → afplay → release
- `enqueue(path)` — non-blocking: spawn detached subprocess that calls `play_audio`

Bash hooks call `vox play <path>` (thin CLI wrapper). The MCP server calls `enqueue()` directly.

### Why fcntl.flock

- Zero infrastructure — no daemon, no message queue, no PID tracking
- Cross-process serialization — works across MCP server, hook scripts, CLI
- Self-cleaning — lock auto-releases on process exit, even crashes
- No audio killed — every utterance succeeds, just queued

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|-----------------|
| PID-based kill-previous (PR #17) | Silences the interrupted speaker — user wants all utterances to succeed |
| Daemon with Unix socket | Operational complexity, lifecycle management, crash recovery |
| Named pipe (FIFO) | Requires a reader process; same daemon problem |
| No coordination (status quo) | Audio overlap is cacophonous |

### Platform Scope

`fcntl.flock` is POSIX (macOS + Linux). The audio player is resolved at
runtime: `afplay` (macOS native) → `ffplay` (cross-platform, from ffmpeg).
ffmpeg is already a project dependency (pydub uses it for audio processing).

---

## DES-014: Dev/Prod Namespace Isolation

**Date:** 2026-02-26
**Status:** SETTLED
**Topic:** How the plugin can be tested from the working tree alongside the installed production plugin

### Problem

`claude --plugin-dir plugin` loads the working tree's plugin surface as a plugin, but it collides with the installed production `vox` plugin if both use the same name. Developers cannot test plugin changes (hooks, commands, MCP tools) without uninstalling the production plugin first.

### Design

The working tree uses `"name": "vox-dev"` in `plugin/.claude-plugin/plugin.json`. Claude Code treats `vox` and `vox-dev` as separate plugins:

- **Prod tools**: `mcp__plugin_vox_vox__speak` (from installed plugin)
- **Dev tools**: `mcp__plugin_vox-dev_vox__speak` (from `--plugin-dir plugin`)

Dev commands are `*-dev.md` files alongside the prod commands in `plugin/commands/` and reference dev-namespaced tools; `session-start.sh` skips them when deploying, and `release-plugin.sh` deletes them for the tag. Prod commands are unchanged.

The MCP server uses the installed `vox` binary as its command. With editable installs (`uv tool install --force --editable .`), the installed binary runs working-tree code — no `uv run` needed.

Release scripts (`scripts/release-plugin.sh`) swap `vox-dev` → `vox` and remove `*-dev.md` files before tagging. `scripts/restore-dev-plugin.sh` reverses this after tagging.

### Session-Start Hook Dispatch

The session-start hook detects dev mode by checking plugin.json for `"vox-dev"`:

- **Dev mode**: skip command deployment (prod plugin deploys top-level commands), auto-allow `mcp__plugin_vox-dev_vox__*`
- **Prod mode**: deploy commands to `~/.claude/commands/`, auto-allow `mcp__plugin_vox_vox__*`

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|-----------------|
| `uv run` in plugin.json/hooks | Unacceptable production dependency — users may not have `uv` installed |
| Single namespace, uninstall prod to test | Destroys the production plugin; cannot test both simultaneously |
| Separate repo for dev plugin | Duplicates code; impossible to keep in sync |

### Key Invariant

The installed `vox` binary always runs working-tree code (via editable install). This means hooks, MCP server, and CLI all exercise the current source without `uv run`.

---

## DES-015: Marketplace Installs from HEAD, Not Tags

**Date:** 2026-02-27
**Status:** SETTLED
**Topic:** Why the v0.4.0 production install was broken and the systemic fix

### Root Cause

Claude Code marketplace installs clone HEAD of the default branch, not the version tag. When a marketplace entry has no `source.ref` field, `claude plugin install` resolves the `version` field for display only — the git clone targets HEAD.

This is invisible when HEAD and the tag are the same commit. It becomes a breaking defect when they diverge — which is exactly what dev/prod namespace isolation does. The release workflow pushes three commits in sequence:

```text
main:  ... → [release] → [prepare: name=vox] → [restore: name=vox-dev]
                              ↑ tag v0.4.0           ↑ HEAD
```

The tag points to the prepare commit (`name: "vox"`). HEAD points to the restore commit (`name: "vox-dev"`). The marketplace installs HEAD — so every user gets the dev plugin.

### Consequences of Installing the Dev Plugin

1. Plugin loads as `vox-dev`, not `vox`
2. Session-start hook detects `DEV_MODE=true`, skips command deployment
3. No top-level `/notify`, `/say`, `/speak`, `/recap`, `/voice` commands
4. User sees only namespaced commands: `/vox-dev:notify`, `/vox-dev:say`, etc.
5. Tool permission auto-allow writes the dev pattern (`mcp__plugin_vox-dev_vox__*`)

The plugin technically works — MCP server starts, audio plays — but the UX is wrong. The user has no idea they're running a dev build.

### Why This Wasn't Caught

1. The developer uses an editable install + `--plugin-dir .`, so the dev name is expected
2. The release script round-trip test verified the scripts work, not the installed artifact
3. No test installs from the marketplace after release — verification step 10 tests PyPI (`tts doctor`), not the plugin
4. Biff has the same dev/prod pattern but its HEAD happened to have the prod name at install time (no release had been cut since adding the pattern)

### Fix (Two Parts)

**Part 1: Pin `source.ref` in marketplace.json.**

Every marketplace entry must specify the release tag:

```json
{
  "name": "tts",
  "source": {
    "source": "github",
    "repo": "punt-labs/vox",
    "ref": "v0.4.0"
  },
  "version": "0.4.0"
}
```

This is required for any project where HEAD of main may diverge from the release tag — which is every project using dev/prod namespace isolation, and arguably every project where post-release commits exist.

**Part 2: Refresh the marketplace clone before plugin install.**

Pinning `source.ref` in the remote marketplace.json only helps when the local clone has the pin. Existing users whose marketplace clone predates the pin see the old marketplace.json without `source.ref` — and `claude plugin install` resolves HEAD again.

The installer must refresh the marketplace clone before running `claude plugin install`, using the supported CLI command:

```python
def _refresh_marketplace() -> StepResult:
    claude = shutil.which("claude")
    if not claude:
        return StepResult("Marketplace refresh", False, "claude CLI not found on PATH")
    result = subprocess.run(
        [claude, "plugin", "marketplace", "update", MARKETPLACE_KEY],
        capture_output=True,
        text=True,
        check=False,
    )
    ...
```

This uses `claude plugin marketplace update` rather than operating on the clone directly, consistent with DES-002 (CLI over config file editing). New users (no clone yet) get a fresh clone with current marketplace.json. Existing users get the latest `source.ref` pins before install.

### Rule

**Every marketplace entry MUST have `source.ref` pinned to the release tag.** The release workflow step 12 (marketplace bump) must update both `version` and `ref`. This is now documented in CLAUDE.md.

**Every installer MUST refresh the marketplace clone before `claude plugin install`.** The `_refresh_marketplace()` step runs `claude plugin marketplace update punt-labs` so existing users pick up ref pins from newer marketplace.json versions.

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|-----------------|
| Don't push restore commit to main | Breaks the dev workflow — developer's working tree would have prod name, defeating namespace isolation |
| Tag HEAD instead of the prepare commit | Tag would include dev artifacts; marketplace clones the tag and gets `vox-dev` anyway |
| File a Claude Code bug to resolve `version` → tag | Correct long-term fix, but we can't control Claude Code's release timeline; `ref` is the available mechanism now |
| Keep main always prod-ready, dev on branches | Every feature branch would need manual plugin.json swap; error-prone, defeats the automation |
| Only pin `source.ref`, skip refresh | Existing users with stale clones never see the pin — install still resolves HEAD |

### Discovery Chain

1. User installed v0.4.0, saw `/vox-dev:notify` instead of `/notify`
2. Checked installed plugin cache: `name: "vox-dev"`, commit `06c2ec7` (restore commit)
3. Compared to v0.4.0 tag: commit `c977c8c` (prepare commit), `name: "vox"`
4. Confirmed: marketplace installed HEAD, not tag
5. Added `source.ref: "v0.4.0"` to marketplace, nuked cache, reinstalled → `name: "tts"`, correct commit
6. Discovered stale clone problem: existing users whose clone predates the ref pin still get HEAD
7. Added `_refresh_marketplace()` to installer — pulls latest marketplace.json before install

---

## DES-016: Command Deployment Must Update, Not Skip-If-Exists

**Date:** 2026-03-05
**Status:** SETTLED
**Topic:** How SessionStart hook deploys top-level commands to `~/.claude/commands/`

### Problem

The SessionStart hook deployed commands with a skip-if-exists guard:

```bash
# WRONG: stale commands persist forever
if [[ ! -f "$dest" ]]; then
  cp "$cmd_file" "$dest"
fi
```

This meant that once a command file was deployed, it **never updated** — even across plugin upgrades and releases. Users accumulated stale command files with:

- Old `allowed-tools` (e.g., `Read`, `Write`, `Edit` instead of `Bash`)
- Old MCP tool names (e.g., `mcp__plugin_tts_vox__speak` from before the rename)
- Old implementation logic (prompt-driven config file editing instead of CLI calls)

The `/vox`, `/unmute`, `/mute`, `/vibe`, and `/recap` commands were all stale. Some still referenced tool names from 3+ releases ago.

### Why This Wasn't Caught

1. The developer uses `--plugin-dir .` (dev mode), which skips command deployment entirely
2. Editable install means the developer's `vox` binary runs working-tree code
3. The stale commands still "worked" — Claude could figure out the intent even with wrong tools — just not correctly
4. No integration test for "install plugin, upgrade, verify commands updated"

### Root Cause

The original skip-if-exists logic was written to be idempotent for first-run setup. The assumption was that commands don't change across releases. That assumption was wrong from day one — commands have changed in almost every release since the plugin launched.

### Fix

Compare content with `diff -q` and overwrite when different:

```bash
# CORRECT: update changed commands
mkdir -p "$COMMANDS_DIR"
if [[ ! -f "$dest" ]] || ! diff -q "$cmd_file" "$dest" >/dev/null 2>&1; then
  cp "$cmd_file" "$dest"
fi
```

### Scope

Fixed in all Punt Labs plugins:

- `vox/hooks/session-start.sh`
- `biff/hooks/session-start.sh`
- `dungeon/hooks/session-start.sh`

Updated in `punt-kit/standards/plugins.md` — the standard now mandates diff-and-update with correct/incorrect code examples.

### Rule

**SessionStart command deployment must always update stale files.** Never skip-if-exists for command deployment. The cost of an unnecessary copy is zero. The cost of a stale command is a broken user experience that persists across every session until the user manually deletes the file.

---

## DES-017: Call Path Performance — MCP over Bash, Hooks over LLM

**Date:** 2026-03-05
**Status:** SETTLED
**Topic:** Which call paths perform best for LLM-initiated and event-driven operations

### Benchmark

Measured 10 sequential calls through each path (apples-to-apples, one model
round-trip per call):

| Path | Avg per call | Why |
|------|-------------|-----|
| **LLM → MCP tool** | ~3.2s | Persistent stdio server, no process spawn. Response is structured JSON. |
| **LLM → Bash → CLI** | ~4.6s | Model round-trip + Python process spawn (~110ms) + text parsing. |
| **Shell hook → CLI** | ~110ms | No model involvement. Direct process execution. |

The model round-trip dominates both LLM paths (~3s of inference per call).
MCP wins over Bash because the server is already running (no spawn cost) and
returns structured data. Shell hooks calling CLI directly are ~30x faster
because they bypass the model entirely.

### Two fast paths

```text
Model-initiated:    LLM ──► MCP server (persistent, structured)
Event-driven:       Hook ──► CLI (no LLM, direct execution)
```

**LLM → MCP** for operations the model initiates: synthesis, voice queries,
config changes. The MCP server is a long-running process — zero startup cost,
structured JSON responses, PostToolUse hooks for UI formatting.

**Hook → CLI** for event-driven operations: stop notifications, permission
chimes, signal tracking. Shell hooks call `vox` CLI directly — no model
round-trip, no inference latency. The hook reads config with grep/sed and
calls the CLI in ~110ms total.

### The slow path (avoid)

```text
LLM ──► Bash ──► CLI    (worst of both worlds)
```

LLM → Bash → CLI combines model round-trip overhead with process spawn
overhead. Every Bash call spawns a fresh Python process (~110ms), and the
model still pays ~3s of inference to generate and parse the call. Use this
only when no MCP tool exists for the operation.

### The Read/Write antipattern (never)

```text
LLM ──► Read(.punt-labs/vox/vox.md)    (file I/O through the model layer)
```

Never instruct the model to Read or Write config files directly. This
couples the model to file format details, bypasses the CLI's validation
logic, and is no faster than an MCP call. If the model needs config state,
either pass it in hook context (zero cost) or expose it via an MCP tool.

### Rule

**Model-initiated operations go through MCP. Event-driven operations go
through shell hooks calling CLI. The model should never touch config files
directly — use the CLI or MCP layer.**

---

## DES-018: Clean Stop Hook Reason — No Internal Data in User-Visible Output

**Date:** 2026-03-06
**Status:** PARTIALLY SUPERSEDED by DES-043 (the signal→tag machinery is gone; the reason-field rule stands)
**Topic:** What the Stop hook's `reason` field contains

> **Partially superseded (DES-043):** the deterministic `resolve_tags_from_signals()` / `vibe_signals` accumulator described here is deleted — auto-vibe is now agent-judged (DES-043). The core rule is unchanged: the Stop-hook `reason` field carries only a user-friendly `♪` phrase, never internal state.

### Problem

The Stop hook's `decision: "block"` response includes a `reason` field that Claude shows to the user as assistant output. Early implementations leaked internal state into this field:

```json
{"decision": "block", "reason": "♪ Saying my piece... | vibe_mode=auto vibe_tags=[calm] vibe_signals=tests-pass@14:32,lint-pass@14:33"}
```

The pipe-separated metadata was intended for the model to thread through to the `unmute` MCP tool. But the entire reason string is displayed in the chat, so users saw raw config data after every task completion.

### Design

The `reason` field contains **only** a `♪`-prefixed phrase — nothing else:

```json
{"decision": "block", "reason": "♪ Saying my piece..."}
```

Vibe tags are resolved deterministically from accumulated signals and written to `vox.local.md` **before** the block response. When Claude calls `unmute` in the continuation turn, `apply_vibe()` reads tags from config automatically. No data passes through the reason string.

```text
Stop hook fires →
  1. resolve_tags_from_signals(config.vibe_signals) → "[relieved]"
  2. write_fields({"vibe_tags": "[relieved]", "vibe_signals": ""})  # atomic
  3. return {"decision": "block", "reason": "♪ Saying my piece..."}

Claude continues →
  4. Generates 1-2 sentence summary
  5. Calls unmute MCP tool → apply_vibe() reads vibe_tags from config
```

### Key Details

- **`resolve_tags_from_signals()`** maps signal counts and trajectory to 1-2 ElevenLabs expressive tags without LLM involvement. Deterministic: same signals always produce the same tags.
- **Signal consumption**: `write_fields()` atomically writes resolved tags AND clears `vibe_signals` in a single config update. This prevents signals from accumulating across stop cycles.
- **Vibe mode gating**: Auto mode resolves and writes tags. Manual mode with existing tags skips (user's choice preserved). Off mode skips entirely.

### Why Config-Mediated Tag Passing

The alternative — embedding tags in the reason string for Claude to extract and pass to the MCP tool — couples the user-visible output to internal data. Any change to tag format or signal structure changes what users see. Config-mediated passing decouples them completely: the hook writes to config, the MCP tool reads from config, and the reason string is free to be a simple human-friendly phrase.

### Rule

**The Stop hook `reason` field must contain only a user-friendly phrase.** No config data, no metadata, no pipe-separated fields. If the continuation turn needs data, write it to config before returning the block response.

---

## DES-019: Bluetooth Audio Lead-In Silence

**Date:** 2026-03-13
**Status:** SETTLED (current solution adequate; alternatives documented for future)
**Topic:** First syllable clipped on Bluetooth audio devices (AirPods)

### Problem

When playing TTS audio after a period of silence (2+ seconds), Bluetooth headphones (AirPods, others) clip the first ~300-500ms of audio. The user hears "...esting one two three" instead of "Testing one two three."

### Root Cause

Bluetooth A2DP audio devices enter a low-power state when no audio is playing. When audio suddenly starts, the Bluetooth controller needs ~300-500ms to:

1. Wake the radio from low-power mode
2. Re-negotiate the audio codec (AAC/SBC)
3. Fill the jitter buffer before playback begins

Audio frames transmitted during this wake-up window are dropped by the device. This is Bluetooth hardware behavior — not fixable in software without compensating for it.

### Why Vox Normally Masks This

In typical sessions, hooks fire frequently enough (chimes on permission prompts, quips on task completion, acknowledgment beeps) that the Bluetooth link stays in active mode. The gap between audio events is usually under 2 seconds — not long enough to trigger low-power transition.

The problem surfaces when there's a deliberate gap: recording → Scribe STT (~700ms) → TTS synthesis (~1000ms) → playback. That ~2 second silence is enough for AirPods to sleep.

### Current Solution

Prepend 500ms of silence to audio before playback:

```python
from pydub import AudioSegment

silence = AudioSegment.silent(duration=500)
speech = AudioSegment.from_mp3(audio_path)
combined = silence + speech
combined.export(padded_path, format="mp3")
```

The silence gives the Bluetooth controller something disposable to drop during wake-up. The user perceives no added latency because the 500ms would have been silent anyway (device was waking up).

### Scope

Currently applied only in the voice-loop spike script (`.tmp/spike-voice-loop.py`). Not yet integrated into the main playback pipeline (`playback.py`), which doesn't have this problem in normal usage because hooks keep the link warm.

### Future Alternatives

If the current approach proves insufficient or the problem surfaces in main playback:

| Approach | Mechanism | Trade-off |
|----------|-----------|-----------|
| **Inaudible keepalive** | Play sub-perceptible tone (~20Hz, -60dB) during synthesis gaps to keep the Bluetooth link active | Prevents the problem entirely; requires background audio thread; may affect battery |
| **CoreAudio device latency query** | Read `kAudioDevicePropertyLatency` and `kAudioDevicePropertySafetyOffset` via CoreAudio API to get actual device latency dynamically | Exact padding per device; macOS-only; requires PyObjC or ctypes bindings |
| **Bluetooth detection** | Query output device type via `sounddevice.query_devices()` or `system_profiler SPBluetoothDataType`; only pad when output is Bluetooth | No wasted silence on wired/built-in speakers; adds platform-specific detection logic |
| **Adaptive padding** | Start with 500ms, measure whether the first syllable is audible (via loopback or user feedback), adjust dynamically | Self-tuning; complex to implement; hard to measure "audibility" programmatically |

### Rule

**When playing audio after a gap of 2+ seconds, assume Bluetooth devices may need wake-up time.** The 500ms silence prefix is the simplest correct solution. Do not reduce it below 500ms without testing on AirPods.

---

## DES-020: Turn-Based Voice Conversation with Claude Code

**Date:** 2026-03-13
**Status:** PROPOSED
**Topic:** Architecture for voice input/output conversation loop in Claude Code

### Goal

Enable turn-based voice conversation with Claude: the user speaks instead of typing, Claude speaks its responses (already works via Stop hook + `/unmute`), and the loop repeats. Voice and keyboard coexist — the user can type a prompt at any time instead of speaking.

### The "Who Presses Enter?" Problem

Claude Code's turn model is user-initiated. Only the user typing and pressing Enter starts a new model turn. No plugin API exists to inject a user prompt programmatically. The voice transcript must somehow trigger Claude to start a new turn.

### Rejected Approaches

| Approach | Why Not |
|----------|---------|
| **Blocking MCP tool** (`listen` tool blocks until user speaks) | MCP tools shouldn't block indefinitely; ties up the tool call; doesn't fit the MCP interaction model |
| **`tools/list_changed` notification** | Delivers the transcript to Claude's tool list, but Claude is idle waiting for user input — the notification doesn't trigger a new turn |
| **User types "go" after speaking** | Defeats the purpose — speaking AND typing is worse than just typing |
| **Stop hook injects transcript as reason** | Only works at task completion boundaries; can't start a cold conversation with voice |

### Proposed Design: Background Task Notification Loop

The solution uses Claude Code's existing background task mechanism (`run_in_background`). A background process blocks until voice input is ready, then exits. Claude receives a `<task-notification>` with the transcript — this triggers a new model turn.

```text
┌─────────────────────────────────────────────────────────────────┐
│ Conversation Loop                                               │
│                                                                 │
│  Claude finishes task                                           │
│    → Stop hook: speaks summary (existing behavior)              │
│    → Claude spawns: `vox listen --wait` (run_in_background)     │
│    → Claude stops                                               │
│                                                                 │
│  User speaks in Lux panel whenever ready                        │
│    → Daemon: mic capture → Scribe STT → transcript ready        │
│    → `vox listen --wait` detects ready → prints transcript      │
│    → exits                                                      │
│                                                                 │
│  Claude receives <task-notification>                            │
│    → reads transcript from task output                          │
│    → acts on the instruction                                    │
│    → spawns next `vox listen --wait`                            │
│    → loop continues                                             │
└─────────────────────────────────────────────────────────────────┘
```

### Key Properties

- **No new Claude Code APIs.** Background tasks and task notifications are existing, proven mechanisms.
- **No timeout pressure.** The listener blocks until the user speaks — 5 seconds or 5 minutes.
- **Self-sustaining loop.** Each task completion spawns the next listener. Stops when voice mode is disabled.
- **Keyboard coexists.** If the user types a prompt before speaking, the background listener is killed or ignored. No conflict.
- **Only when enabled.** The listener is spawned only when voice input mode is active. Normal sessions are unaffected.

### Components

```text
┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│   Lux Panel      │     │   Vox Daemon     │     │   Claude Code    │
│                  │     │   (mcp-proxy)    │     │                  │
│ [🎤 Record]      │────▶│ mic capture      │     │                  │
│ [⏹ Stop]        │     │ Scribe STT       │     │                  │
│                  │     │ transcript store  │     │                  │
│ Transcript:      │◀────│                  │     │                  │
│ "refactor auth"  │     │                  │     │                  │
│                  │     │                  │     │                  │
│ [Send] [Discard] │────▶│ ready=true       │     │                  │
│                  │     │                  │────▶│ task-notification │
└──────────────────┘     │                  │     │ "refactor auth"  │
                         │ `listen --wait`  │     │                  │
                         │ (blocks, then    │     │ → acts on it     │
                         │  prints + exits) │     │ → spawns next    │
                         └──────────────────┘     │   listener       │
                                                  └──────────────────┘
```

### `vox listen --wait` Command

Thin CLI command that:

1. Connects to the vox daemon (WebSocket or polling)
2. Blocks until a transcript is marked ready
3. Prints the transcript to stdout
4. Exits with code 0

If the daemon is unreachable or voice mode is disabled, exits immediately with code 1. No retry, no backoff — the caller (Claude) decides whether to respawn.

### Lux Voice Panel

The Lux window provides the visual interface for recording:

- **Record button** — starts mic capture via the daemon
- **Stop button** — ends recording, triggers Scribe transcription
- **Transcript display** — shows the transcribed text for review
- **Edit field** — user can correct Scribe mistakes before sending
- **Send button** — marks transcript as ready (unblocks `listen --wait`)
- **Discard button** — clears transcript, returns to idle state

### Prerequisites

| Dependency | Status |
|------------|--------|
| ElevenLabs Scribe STT | Proven in spike (700ms latency for 5s clip) |
| ElevenLabs TTS playback | Shipping (existing provider) |
| Bluetooth lead-in silence (DES-019) | Proven in spike (500ms padding) |
| Mic capture (`sounddevice`) | Proven in spike |
| Lux interactive elements | Available (buttons, text inputs, recv) |
| mcp-proxy daemon model | Shipped (DES-021) |
| `vox listen --wait` CLI command | Not built |
| Daemon-side transcript store | Not built |

### Phasing

1. **Spike (done):** Prove mic → Scribe → TTS round-trip works (`.tmp/spike-voice-loop.py`)
2. **mcp-proxy migration (done, DES-021):** Move vox to the daemon model so the daemon can own the mic, Scribe client, Lux connection, and transcript store
3. **`listen --wait` command:** CLI that blocks on the daemon's transcript-ready signal
4. **Lux voice panel:** Record/stop/send UI in the Lux window
5. **Conversation loop integration:** Stop hook spawns `listen --wait` in background when voice mode is active

### Open Questions

1. **How does the first turn start?** The loop is self-sustaining once running, but the initial `listen --wait` needs to be spawned. A `/voice` command could spawn the first listener.
2. **Concurrent listeners:** If the user types a prompt while `listen --wait` is running, the background task should be killed or its result ignored. How does Claude handle a stale task notification that arrives after a typed prompt?
3. **VAD vs button:** The Lux panel uses explicit Record/Stop buttons. Future enhancement: VAD-based auto-detection (start recording on speech, stop on silence) for hands-free operation.
4. **Error recovery:** If Scribe fails or returns garbage, the user sees it in the Lux panel and can discard. But `listen --wait` should also handle daemon disconnection gracefully.
5. **Multi-session:** With the daemon model, multiple Claude Code sessions could have voice mode enabled. The daemon needs per-session transcript state (keyed by session_key from mcp-proxy).

---

## DES-021: Daemon Mode — Single Process with mcp-proxy

**Date:** 2026-03-14
**Status:** SUPERSEDED by DES-028

> **Note:** This ADR describes the v2 mcp-proxy daemon design. The production implementation now uses `voxd` (DES-028) — a simpler audio server without mcp-proxy, ContextVar, or PID-based CWD resolution.

**Topic:** Convert vox from per-session MCP processes to a single daemon

### Problem

Each Claude Code session spawns its own `vox mcp` process (~19MB each). With 10+ sessions, that's 10+ independent processes, each with its own TTS provider, playback queue, and hook handlers. Three concrete problems:

1. **Duplicate audio**: `biff wall` sends the same notification to all sessions → each synthesizes and plays identical TTS independently
2. **Resource waste**: 10+ Python processes doing the same work
3. **Hook latency**: Each hook invocation cold-starts Python (~500ms) to call `vox hook <event>`

### Design

Single long-running daemon fronted by mcp-proxy (same pattern as quarry, DES-020 prerequisite):

```text
MCP bridge (long-lived, per-session):
                    stdio                      WebSocket
Claude Code ◄──────────────► mcp-proxy ◄──────────────────────► vox serve
             MCP JSON-RPC    (~6MB Go)       ws://localhost:8421  (one daemon)
                                              /mcp

Hook relay (one-shot, per-event):
                    stdin/stdout                WebSocket
Hook script ──────────────────► mcp-proxy ──────────────────────► vox serve
             JSON payload       (~15ms)        ws://localhost:8421  (same daemon)
                                               /hook
```

Falls back to `vox mcp` (stdio) and `vox hook <event>` (subprocess) when daemon/mcp-proxy unavailable.

### Key decisions

**Starlette ASGI over plain WebSocket server** — Reuses the pattern from quarry's `http_server.py`. Starlette provides routing, lifespan management, and test client support. uvicorn handles signal handling and graceful shutdown.

**ContextVar for per-session config isolation** — Each MCP WebSocket connection sets `_config_path_override` via ContextVar so `resolve_config_path()` returns the correct project's `.vox/config.md` without passing paths through every function. The ContextVar is reset when the connection closes.

**CWD resolution from PID** — When a session connects with `?session_key=<pid>`, the daemon looks up the process's cwd via `lsof` (macOS) or `/proc/<pid>/cwd` (Linux) to find the right `.vox/config.md`. This is resolved once and cached in the session registry.

**Audio deduplication** — `DaemonContext.should_play(cache_key)` returns False if the same notification type was played within 5 seconds. Checked on the event loop thread (before `asyncio.to_thread` dispatch) to avoid data races. Prevents biff-wall duplicate audio across sessions.

**Graceful fallback** — Plugin.json uses `sh -c "if command -v mcp-proxy; then exec mcp-proxy ws://...; else exec vox mcp; fi"`. Hook scripts try `mcp-proxy --hook` first, fall back to `vox hook <event>`. Users without mcp-proxy or without the daemon running get identical behavior to before.

### Alternatives considered

1. **Unix domain socket instead of WebSocket** — Simpler but mcp-proxy speaks WebSocket natively. UDS would require a custom transport.
2. **Shared process group** — Use multiprocessing to share state. Too fragile across crash/restart cycles.
3. **Redis/IPC for dedup** — Over-engineered. The daemon is single-process; a dict with monotonic timestamps is sufficient.

### Files

- `src/punt_vox/daemon.py` — Starlette app with /mcp, /hook, /health
- `src/punt_vox/service.py` — launchd/systemd service management
- `src/punt_vox/config.py` — Added `_config_path_override` ContextVar
- `src/punt_vox/server.py` — Added `run_mcp_session()` for WebSocket transport
- `src/punt_vox/__main__.py` — `vox serve`, `vox daemon install/uninstall/status`
- `.claude-plugin/plugin.json` — mcp-proxy fallback
- `hooks/*.sh` — Daemon-first relay with subprocess fallback

---

## DES-022: AskUserQuestion Works Inside Slash Commands

**Date:** 2026-03-20
**Status:** SETTLED (verified, test artifact removed)
**Topic:** Whether `AskUserQuestion` renders inside skill/command execution

### Finding

A test command (`commands/ask-test-dev.md`) verified that `AskUserQuestion` with options renders correctly inside a slash command. The tool presents a picker UI and returns the selected option. This confirms commands can use interactive prompts for user input, not just static instructions.

### Outcome

Test passed. The `ask-test-dev.md` scaffold was removed — it served its purpose and has no production value. Commands that need user choices (e.g., voice selection in `/unmute`) can use `AskUserQuestion` with confidence.

---

## DES-023: Assets Bundled in Python Package

**Date:** 2026-03-28
**Status:** SETTLED
**Topic:** How chime MP3 files are distributed and resolved at runtime

### Problem

Chime audio never played in daemon mode or from the installed `vox` binary. `_resolve_assets_dir()` had two strategies:

1. `CLAUDE_PLUGIN_ROOT` env var → `$CLAUDE_PLUGIN_ROOT/assets/` — works for Claude Code hook scripts
2. `Path(__file__).parent.parent.parent / "assets"` → walks up to repo root — works for editable installs from the source tree

Strategy 2 fails for the installed package: `__file__` resolves to `site-packages/punt_vox/hooks.py`, so `.parent.parent.parent` = `site-packages/`, and `site-packages/assets/` doesn't exist. The daemon process runs from the installed binary with no `CLAUDE_PLUGIN_ROOT`, so every chime resolution failed silently ("missing chime_done.mp3" in logs).

### Design

Move canonical assets into the Python package: `assets/` → `src/punt_vox/assets/` (subpackage with `__init__.py`). `uv_build` auto-discovers and includes them in the wheel.

Fallback path becomes `Path(__file__).resolve().parent / "assets"` — sibling to the module files. Works for editable installs, installed packages, and daemon mode.

A symlink at repo root (`assets` → `src/punt_vox/assets`) preserves the `CLAUDE_PLUGIN_ROOT/assets/` resolution path for Claude Code hook scripts.

### Why Subpackage, Not `data` Config

`uv_build`'s `data` directive installs files into a platform-specific `.data/` directory in the wheel, not alongside the Python modules. Files there aren't findable via `__file__`-relative paths. Making `assets/` a subpackage (with `__init__.py`) puts the MP3s directly in `site-packages/punt_vox/assets/`, co-located and trivially resolvable.

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|-----------------|
| `importlib.resources` | Adds complexity; `Path(__file__).parent / "assets"` is simpler and works for all cases |
| Copy assets at install time via post-install hook | `uv` doesn't support post-install hooks; fragile |
| Resolve from Claude plugin installation directory | Fragile — depends on knowing the plugin install path, which varies |
| Keep assets at repo root, fix `__file__` traversal | Three `.parent` calls is fragile and breaks when package layout changes |

---

## DES-024: Daemon Lifecycle — Kill Process on Uninstall, Detect Stale on Install

**Date:** 2026-03-28
**Status:** SETTLED
**Topic:** How `vox daemon install` and `vox daemon uninstall` handle running processes

### Problem

Two lifecycle bugs discovered while deploying the DES-023 asset fix:

1. **`vox daemon uninstall`** removed the launchd plist but left the daemon process running. The old process continued serving on port 8421 with pre-fix code, invisible to the user.

2. **`vox daemon install`** did not detect a stale process occupying port 8421. `launchctl load` failed silently (or the new process couldn't bind), but `_launchd_status()` showed the service as "loaded" — so install reported success while the old process kept running.

Both bugs compound: uninstall leaves a zombie, install doesn't detect it, user thinks they upgraded but nothing changed.

### Design

A shared `_kill_stale_daemon()` helper used by both install and uninstall:

```text
_kill_stale_daemon():
  1. Read port from ~/.punt-labs/vox/serve.port (fallback: DEFAULT_PORT)
  2. Find PID via lsof -ti :<port> (macOS) or fuser <port>/tcp (Linux)
  3. SIGTERM → wait up to 5s → SIGKILL if still alive
  4. Remove serve.port (serve.token is preserved for session continuity)
```

- **Uninstall** calls `_kill_stale_daemon()` after removing the service config
- **Install** calls `_kill_stale_daemon()` before registering the new service

### Why SIGTERM-then-SIGKILL

The daemon runs a Starlette ASGI server with active WebSocket connections. SIGTERM triggers uvicorn's graceful shutdown (closes connections, runs lifespan shutdown). SIGKILL is the fallback for hung processes — 5 seconds is generous for a local daemon with no persistent state.

### Why Not Just `launchctl kickstart`

`launchctl kickstart -k` can restart a service, but it requires the service to be loaded. If the plist was removed (uninstall) or never loaded (stale process from a previous install), kickstart has nothing to act on. Directly killing the process is the only reliable approach.

## DES-025: Daemon Provider Key Resolution via keys.env

**Date:** 2026-03-28
**Status:** SETTLED
**Topic:** How the daemon gets API keys for TTS providers when launchd/systemd strip the shell environment

### Problem

The vox daemon runs as a launchd (macOS) or systemd (Linux) service. These init systems start processes with a minimal environment — no direnv, no shell profile, no API keys. Without `ELEVENLABS_API_KEY` or `OPENAI_API_KEY`, the daemon falls back to `say` (macOS) or `espeak` (Linux) — system TTS that sounds terrible.

Before the daemon existed, vox ran inside Claude Code's process and inherited the shell environment. The daemon broke that model.

### Rejected Alternatives

1. **Embed API keys in the launchd plist / systemd unit** — Keys would be visible in the service config file. More importantly, the plist is written at install time from whatever shell runs `vox daemon install`. If that shell doesn't have direnv loaded (e.g., running from a directory without `.envrc`), no keys get embedded. Fragile and non-obvious.

2. **Pass keys per-request via MCP protocol** — Would preserve the "your shell controls your provider" model, but adds complexity to the MCP wire protocol, requires changes to every MCP tool, and means the daemon can't auto-detect providers at startup. Also raises questions about key transit security over localhost WebSocket.

3. **Read keys from macOS Keychain / Linux secret-service** — Not portable. Keychain is user-specific setup, not something every vox user would have. Not a general solution.

### Design

A dedicated config file at `~/.punt-labs/vox/keys.env` (same path on macOS and Linux). Simple `KEY=VALUE` format, chmod 0600.

**Write path:** `vox daemon install` calls `_write_keys_env()` in service.py. This snapshots provider-relevant env vars (`ELEVENLABS_API_KEY`, `OPENAI_API_KEY`, `AWS_*`, `TTS_PROVIDER`, `TTS_MODEL`) from the caller's shell into the per-user config file at `~/.punt-labs/vox/keys.env`. Runs as the installing user — no sudo for the file write.

**Read path:** `voxd` calls `_load_keys()` at startup, before logging or provider auto-detection. Sets `os.environ` for keys not already present. This means:

- launchd/systemd daemon: loads all keys from file (nothing in env)
- Manual `voxd` from shell with direnv: env vars already set, file keys ignored

**Resolution order:** shell env var > keys.env value > provider unavailable.

### Why a Flat File, Not TOML/YAML

The file is never sourced by a shell. It's parsed by a 15-line Python function. No quoting, no escaping, no schema. One `KEY=VALUE` per line, `#` comments, blank lines ignored. The simplest format that works.

## DES-026: Stable Auth Token Across Daemon Restarts

**Status:** SETTLED

### Problem

The daemon generated a fresh auth token (`secrets.token_urlsafe(32)`) on every startup. Clients already connected held the old token. When the daemon restarted, all existing client connections failed authentication.

### Decision

The auth token is generated once and persisted to `<run_dir>/serve.token` (chmod 0600). It is stable across daemon restarts. The run dir is `~/.punt-labs/vox/run/` on both macOS and Linux.

- `voxd` startup (`_read_or_create_token()`): reads the token from file. If the file is missing, generates and persists one.
- The token file is NOT removed on daemon shutdown (unlike `serve.port`, which is removed to signal the daemon is down).

### Why Not Remove Auth Entirely

The daemon binds to `127.0.0.1`, so only local processes can connect. Removing auth was considered (same trust model as Docker daemon default, Redis default). Rejected because:

1. Multi-user systems: other users on the same machine could connect.
2. Defense in depth: the token costs nothing and prevents accidental tool invocation by other local MCP clients.
3. The token mechanism already exists and works — removing it is churn with no upside.

### Rejected: Token Rotation on Every Install

The original design (regenerate on install) was simpler but broke the reconnection invariant. mcp-proxy's reconnect logic (exponential backoff, caps at 5s) works correctly only when the URL is stable. Changing the token on install means the daemon must either: (a) accept both old and new tokens during a grace period, or (b) require all clients to re-read the token file. Both are more complex than simply keeping the token stable.

## DES-027: Data Directory Migration to ~/.punt-labs/vox/

**Status:** SETTLED (reinstated after DES-028 rollback)

> **Note:** DES-027 migrated daemon data from `~/.punt-vox/` to `~/.punt-labs/vox/`. DES-028 (v3) briefly moved daemon data to system paths (`/etc/vox/`, `/var/log/vox/`, `/var/run/vox/` on Linux; Homebrew-prefix equivalents on macOS), but that move stranded user API keys on upgrade and required sudo to edit personal tokens. It was rolled back in the v4.x branch and daemon state lives under `~/.punt-labs/vox/` on both platforms again. See DES-028 for the settled state and the rollback rationale.

## DES-028: Vox v3 — Audio Server Architecture

**Status:** SETTLED

### Problem

The v2 daemon tried to know which project a client belonged to. It resolved CWDs from PIDs via `lsof`, read/wrote `.vox/config.md` in project directories, and used ContextVars to isolate per-session config. Every piece of this chain broke — 8 rounds of path bugs. The root cause was architecture, not code.

### Decision

One machine, one set of speakers, one audio daemon (`voxd`). Clients send text + parameters. The daemon synthesizes and plays. It knows nothing about projects, sessions, CWDs, or Claude Code.

**Two entry points, one package:**

- `voxd` — per-user audio daemon. Owns speakers, providers, playback queue, dedup, cache. All daemon state lives under `~/.punt-labs/vox/` on both macOS and Linux.
- `vox` — everything else. CLI, MCP server (`vox mcp`), hook handlers. All are clients of `voxd`.

**Wire protocol:** WebSocket + JSON messages. Streaming-capable for future real-time voice conversation.

**MCP server:** Lightweight stdio process per Claude Code session. Session state in memory. Finds `.punt-labs/vox/` by walking up from CWD (same as biff). Reads `vox.md` (durable prefs) and `vox.local.md` (ephemeral state). Calls `voxd` via WebSocket for synthesis/playback. No provider imports — cold start < 500ms.

**Hooks:** Three-layer dispatch unchanged (hooks.md standard). Python handlers call `voxd` via WebSocket client. No in-process synthesis.

**Service install:** macOS: `~/Library/LaunchAgents/com.punt-labs.voxd.plist` — user-level LaunchAgent, no sudo required (migrated from `/Library/LaunchDaemons/` in DES-038). Linux: `/etc/systemd/system/voxd.service` with `User=` installing user — sudo required to place the unit file. All per-user state under `~/.punt-labs/vox/` is created with normal user permissions on both platforms.

### Why Not Keep the Proxy Architecture

mcp-proxy existed to avoid spawning a Python process per session. The new MCP server is lightweight (no provider imports) so Python startup cost is acceptable. Eliminating mcp-proxy removes a Go binary dependency, the WebSocket MCP bridge, and the entire class of "MCP session doesn't survive daemon restart" bugs.

### Why WebSocket, Not HTTP

HTTP request/response can't do bidirectional streaming. Real-time voice conversation (vox-7hr) needs streaming audio in both directions. WebSocket handles both fire-and-forget synthesis (today) and streaming conversation (future) without a protocol change.

### System Paths

All per-user state lives under the installing user's home dir — same layout on macOS and Linux. Only the system service unit lives in a platform-specific system directory.

| Purpose | Path |
|---------|------|
| Config (API keys) | `~/.punt-labs/vox/keys.env` |
| Logs | `~/.punt-labs/vox/logs/voxd.log` |
| Runtime (port, token) | `~/.punt-labs/vox/run/serve.{port,token}` |
| Cache | `~/.punt-labs/vox/cache/` |
| Service (macOS) | `~/Library/LaunchAgents/com.punt-labs.voxd.plist` (DES-038) |
| Service (Linux) | `/etc/systemd/system/voxd.service` |

### Why Per-User Paths, Not System Directories

The v3 rewrite (DES-028 original) tried FHS system paths (`/etc/vox/`, `/var/log/vox/`, `/var/run/vox/`) on Linux and Homebrew-prefix equivalents on macOS. That was wrong: `voxd` runs as a single user (`User=` in the systemd unit, `UserName` in the launchd plist), so its state is per-user, not system-shared. The system-path model stranded existing users' API keys on upgrade, required sudo to edit personal tokens, and created a chown mismatch where the file voxd was told to read was owned by root. State now lives under `~/.punt-labs/vox/` on both platforms — same as any other per-user daemon (`~/.ssh`, `~/.gnupg`, `~/.aws`, and the other Punt Labs agent tools under `~/.punt-labs/`).

### Service Identity

`voxd` runs as the installing user, not root. Audio device access (CoreAudio on macOS, PulseAudio/PipeWire on Linux) is tied to the desktop session user. The LaunchDaemon plist sets `UserName` to the installing user; the systemd unit sets `User=` to the installing user. `vox daemon install` itself runs as the normal user and refuses to start under `sudo` — it prompts for a sudo password only to place the unit/plist file into its system directory and to reload the daemon manager. Every per-user file is created with normal user permissions. See DES-029 for the privilege-scoping rationale.

## DES-029: Scope `sudo` to System Service Installation Only

**Status:** SETTLED

### Problem

The initial v4 `vox daemon install` ran the entire install command under `sudo`. The CLI wrapper was `sudo vox daemon install` and the Python code was left to handle "I am running as root but the data belongs to $SUDO_USER." That meant: reading `SUDO_USER` from the environment, resolving the target user's home dir via `pwd.getpwnam`, chowning every created directory back to that user, opening `keys.env` with `O_NOFOLLOW|O_EXCL|O_CREAT`, verifying the open descriptor with `fstat`, calling `fchown` on the descriptor, rejecting symlinks anywhere in the ancestor chain — an increasingly baroque pile of privilege-defense code whose only purpose was to protect root from a user-controlled directory tree.

Each review round added another layer: Cursor Bugbot found that chowning `state_root.parent` (`~/.punt-labs`) could hand root-owned system paths to the user if the parent was a symlink, so the code added an explicit parent-symlink check. Another round found that `O_TRUNC` without `O_NOFOLLOW` could redirect the privileged write to `/etc/shadow`, so the code added `O_NOFOLLOW`. Another round found that the plist baked in `/var/root/.punt-labs/` paths because `Path.home()` under `sudo` pointed at root's home, so the code added `_user_state_dir_for(target_user)`. The stack kept growing. No finding was invalid — all of them were real — but each one was paying down interest on the wrong architectural choice.

### Rejected Alternatives

1. **Keep root-inside-$HOME and harden each review-cycle finding individually** — sustainable only as long as review rounds keep finding every hole. Symlink/TOCTOU/chown-ordering bugs compound quickly in privileged code. The surface was already three layers deep (path walk, `O_NOFOLLOW`, `fchown` on the fd) and still growing.
2. **Use `sudo -u $SUDO_USER` to re-exec the user-owned portion** — gets the permissions right but introduces two process boundaries mid-command, complicates error propagation, and still leaves "the parent process runs as root" as the user's observable reality.

### Decision

`vox daemon install` runs as the invoking user from start to finish. The command refuses to run under `sudo` (`os.geteuid() == 0` check at the top of `install()`). All per-user filesystem writes under `~/.punt-labs/vox/` happen with normal user permissions — no chown, no `fchown`, no `O_NOFOLLOW`, no symlink walks, no `SUDO_USER` lookup. The privileged surface shrinks to five `subprocess.run(["sudo", ...])` calls on Linux and four on macOS, each touching only a system directory the user could not write to anyway:

**Linux (5 calls):**

1. `sudo systemctl stop voxd` (pre-flight, skipped on fresh install)
2. `sudo install -m 644 -o root -g root <tmp> /etc/systemd/system/voxd.service`
3. `sudo systemctl daemon-reload`
4. `sudo systemctl enable voxd`
5. `sudo systemctl restart voxd`

**macOS (0 calls — DES-038):**

DES-038 moved the macOS plist from `/Library/LaunchDaemons/` to `~/Library/LaunchAgents/`. LaunchAgents are user-owned — no sudo required for any steady-state operation. The one-time migration from the old LaunchDaemon uses 2 sudo calls (`unload` old plist + `rm` old plist), then never again.

The unit/plist content is written directly to `~/Library/LaunchAgents/` (macOS, user-writable) or to a user-owned tmp file then placed via `install(1)` into `/etc/systemd/system/` (Linux, root-writable).

### Why the Pre-flight Stop

Review round 3 (Cursor Bugbot 3048416720) found that `install()` was calling `_ensure_port_free` (which issues a direct `os.kill(SIGTERM)` to the stale voxd PID) before running the platform-specific install path. On macOS, launchd's `KeepAlive=true` immediately respawned the killed daemon with the OLD plist; on Linux, systemd's `Restart=on-failure` treated the kill as a failure exit and restarted the process under the old unit. The upgrade flow was racing against the service manager.

The fix is a pre-flight stop through the service manager (`_launchd_stop` on macOS, `_systemd_stop` on Linux) BEFORE `_ensure_port_free` runs. That tells the manager "I am going to kill this, do not respawn it." The subsequent port check is then idempotent: anything still listening is stale state that survived a manager crash and is safe to kill outright. Both pre-flight helpers are idempotent — fresh installs with no prior unit file skip the sudo call entirely, so the fresh-install shape is 4 calls on Linux and 3 on macOS (pre-flight is a no-op; unit write + reload + enable + restart for Linux, install + load + kickstart for macOS).

### Why Restart, Not Enable --now

Review round 2 found that `systemctl enable --now` does not restart an already-running service, so on upgrade the running voxd would keep the stale `ExecStart` baked in from the previous unit. The Linux install shape uses `enable` + `restart` as separate primitives: `enable` is the boot-persistence step (idempotent), `restart` is the unconditional cycle. The macOS shape adds `launchctl kickstart -k` after `load` — `load` on an already-loaded plist is a no-op and does not restart the daemon, so `kickstart -k` is the only primitive that forces a reload of the new `ExecStart`.

### Why Refuse `sudo` Instead of Silently Demoting

If a user runs `sudo vox daemon install` out of habit, the three wrong things happen: `getpass.getuser()` returns `root`, `Path.home()` resolves to `/root`, and all per-user state lands under `/root/.punt-labs/vox/` (invisible to the normal user, and the generated systemd unit has `User=root`, so the daemon runs as root and loses audio device access). Silently demoting with `os.seteuid` + `os.setegid` would fix the ownership but would leave the habits unchanged — the user would still run the command wrong next time, and the failure mode would become "works on my machine." Explicit refusal with a clear error message retrains the habit.

### Why This Deletes More Code Than It Adds

The initial refactor commit shipped -443 net lines across 10 files. The deletions were all the defensive code that no longer has anything to defend: `_reject_symlinks`, `_chown_to_user`, `_user_keys_env_file_for`, `_user_state_dir_for`, `_installing_user`, the `target_uid`/`target_gid` parameters of `_write_keys_env`, the `_open_new`/`_open_existing`/`O_NOFOLLOW`/`O_EXCL`/`fstat`/`fchown` dance, the `SUDO_USER` environment lookup, the parent-symlink check, the `os.lchown` calls in `_ensure_user_dirs`, and every test that exercised those code paths. The only defensive code that survived is the control-character validation in `_write_keys_env` (rejecting `\n`/`\r`/`\x00` in env values) — that is input sanitization, not a privilege defense, and applies equally when the process runs as the user.

## DES-030: Music Playback — Separate Subprocess at Reduced Volume

**Status:** SETTLED

### Problem

Music tracks loop for minutes. The existing `_playback_consumer` queue and `_playback_mutex` handle short audio (chimes, TTS) with a 30-second timeout. If music used the same queue, it would hold the mutex for the entire track duration, blocking all chimes and speech.

### Rejected Alternatives

1. **SIGSTOP/SIGCONT** — pause the music subprocess when speech needs to play, resume after. POSIX-portable, but creates an unnatural silence gap. Users don't stop their music when someone talks to them; they turn it down.
2. **PulseAudio/PipeWire dynamic ducking** — lower the music stream's volume via `pactl set-sink-input-volume` when speech fires. Correct UX, but requires runtime PulseAudio/PipeWire detection, stream identification, and volume state management. Complexity disproportionate to v1.
3. **Shared playback queue with long timeout** — raise the timeout to 5 minutes. Simple, but blocks all TTS for the entire track duration since `_playback_mutex` is held.

### Decision

Music plays via its own ffplay subprocess at `-volume 30` (Linux) / `--volume 0.3` (macOS), completely outside the existing playback queue. Speech and chimes play at full volume through the normal queue and overlay on top. No pausing, no ducking, no mutex contention. The volume differential makes speech intelligible over the background music without any runtime coordination. Dynamic ducking via PulseAudio is a future enhancement.

## DES-031: Music Session Ownership Model

**Status:** SUPERSEDED by DES-041 (ownership removed)

> **Superseded (DES-041):** the Audio Programs Program model removed session ownership entirely — Program state is machine-universal and any client drives any command; the `owner_id` / reject-non-owner gate described here (source of the vox-73m5 stale-vibe bug) is gone.

### Problem

voxd is shared across all Claude Code sessions and CLI users. Music is daemon-wide (one set of speakers, one music loop). When multiple sessions are active, which session's vibe drives the music?

### Rejected Alternatives

1. **Last-active session wins** — whichever session most recently changed its vibe sends that to voxd. Simple, but jarring: switching terminals flips the music based on which one you last typed in.
2. **Music has its own vibe, independent of per-repo vibe** — `/music on style techno` sets a music-specific mood on voxd. Per-repo `/vibe` stays separate. Simplest to implement but loses the reactive-to-vibe behavior.

### Decision

The session that runs `/music on` **owns** the music. That session's vibe drives the music prompt. Other sessions' vibe changes do not affect the music. Ownership transfers when another session explicitly runs `/music on` (which claims it) or `/music off` (which stops it). Each MCP server generates a `session_id` (UUID) at startup, sent as `owner_id` with every music message. voxd rejects `music_vibe` messages from non-owning sessions.

## DES-032: Duration-Proportional Playback Timeout

**Status:** SETTLED

### Problem

`_PLAYBACK_TIMEOUT_S = 30.0` was a fixed constant that killed ffplay after 30 seconds. Set when vox only played short chimes and quips. A 480-character recap generates 34.3 seconds of speech at ElevenLabs default rate — the timeout fires at 87%, cutting mid-word. Any TTS over ~450 characters is silently truncated.

### Rejected Alternatives

1. **Raise the fixed timeout to 120s** — simple, but leaves a hard ceiling that longer content will eventually hit again. Also means a stuck ffplay process takes 2 minutes to detect instead of 30 seconds.
2. **No timeout** — removes the safety net for hung processes entirely. A single stuck ffplay would block the playback queue permanently.

### Decision

Probe the file duration via `ffprobe -v quiet -show_entries format=duration` before spawning the player. Set timeout to `max(duration + 10s, 30s)`. A 34s file gets 44s. A 2-minute file gets 130s. Short files keep the 30s floor. Probe failure degrades gracefully to the 30s default. The probe runs in <10ms for local files and adds negligible latency.

## DES-033: Gapless Music Handoff on Vibe Change

**Status:** SUPERSEDED by DES-039

> **Superseded (DES-039):** the mid-generation concurrent-handoff (`music_changed` race + generation `asyncio.Task`) is replaced by the self-driving playlist's eager background fill — a vibe change finishes the current track while the pre-filled next pool is already on disk, so there is nothing to wait for at the handoff.

### Problem

When the session vibe changes while music is playing, a new track must be generated (~10-30s). The naive approach — kill the old track, generate, play the new one — creates an audible silence gap during generation.

### Rejected Alternatives

1. **Kill immediately, accept the silence** — the first implementation (PR #194 commit ae79d9f). Simple but produces 10-30s of dead air every time the vibe changes. Users expect continuous background music.
2. **Break out of the playback loop, generate, then restart** — old track finishes its current iteration but doesn't re-loop during generation. If generation takes longer than the remaining track duration, silence returns. Also orphans the ffplay subprocess (Bugbot caught this).
3. **Pre-generate the next track speculatively** — generate a track for every possible vibe in advance. Wastes credits on tracks that may never play.

### Decision

Run generation as a concurrent `asyncio.Task` while the playback loop continues looping the old track. On each playback iteration, the loop races `proc.wait()`, `music_changed.wait()`, and the generation task. Handoff (kill old proc, switch to new track) happens only when the generation task completes. A second vibe change during generation cancels the in-flight task and starts a fresh one — old track keeps looping throughout. The old track plays continuously from the moment `/music on` fires until `/music off` or a new track is ready.

## DES-034: Peer-Closed WebSocket — State Check vs Widened Exception

**Status:** SETTLED

### Problem

After the vox-ehf fix in v4.3.0, chime/unmute clients return on the `"playing"` ack and close the WebSocket. The next `receive_text()` call raises `RuntimeError` (not `WebSocketDisconnect`), logging a full traceback on every chime.

### Rejected Alternatives

1. **Widen the except clause to `(WebSocketDisconnect, RuntimeError)`** — the initial fix (PR #185 commit a191a3c). Correct for the specific case, but catches *any* RuntimeError in the handler chain. Copilot flagged it: a future handler raising RuntimeError for a real bug would be silently swallowed. The widened surface was unnecessarily broad for a fix that only needed to handle the disconnect state.

### Decision

Check `websocket.application_state != WebSocketState.CONNECTED` at the top of the receive loop, before `receive_text()` is called. If disconnected, `break` cleanly. The outer `except` clause stays narrow (`WebSocketDisconnect` only). A genuine `RuntimeError` from a handler still surfaces as an ERROR log. Two complementary tests document the narrowing guarantee: one verifies the state check preempts a disconnected-socket error, the other verifies an unexpected RuntimeError still logs as an error.

## DES-035: Track Naming and Zero-Credit Replay

**Status:** SETTLED

### Problem

Generated music tracks are saved to `~/Music/vox/tracks/` but only identifiable by timestamped filenames. Users can't find a track they liked, can't replay it without regenerating (burning credits), and can't build a personal library.

### Rejected Alternatives

1. **Hash-based naming** — name tracks by content hash (MD5/SHA256 of the audio). Unique and collision-free, but human-unreadable. A user can't find "that techno track from Tuesday" by scanning filenames.
2. **No replay — always regenerate** — simplest implementation, but ElevenLabs generation is non-deterministic (same prompt produces different tracks). A track the user liked is gone forever once the loop moves on. Also wastes ~2000 credits per replay.

### Decision

Auto-name tracks as `{vibe}-{style}-{YYYYMMDD-HHMM}` (e.g. `happy-techno-20260412-1118`). Users can provide custom names via `/music on --name late-night-flow`. When a name matches an existing file in `~/Music/vox/tracks/`, skip generation entirely and loop the saved track — zero credits, instant playback. `/music play <name>` replays any saved track. `/music list` shows the library with name, size, and date. The `music_replay` flag in `DaemonContext` tells `MusicLoop` to skip generation and go straight to the playback loop.

## DES-036: Config Split — Durable Prefs vs Ephemeral State

**Date:** 2026-05-11
**Status:** SETTLED
**Topic:** Why per-repo config is two files instead of one

### Problem

The single `.vox/config.md` mixed durable preferences (voice, provider, notify mode) with ephemeral session state (current vibe, vibe tags, accumulated signals). This caused two problems:

1. **Tracked/untracked conflict.** Users wanted to commit their voice and provider preferences (team defaults), but `vibe_signals` changes every few seconds during a session — committing the file would produce constant noise.
2. **Directory location.** `.vox/` was a non-standard location. The org filesystem standard puts per-tool config under `.punt-labs/<tool>/`.

### Design

Two files under `.punt-labs/vox/`:

- **`vox.md`** — tracked in git. Durable preferences: `voice`, `provider`, `model`, `notify`, `speak`, `vibe_mode`. These are team-sharable defaults.
- **`vox.local.md`** — gitignored. Ephemeral session state: `vibe`, `vibe_tags`, `vibe_signals`. These change during a session and have no value across sessions.

Field routing is explicit: `DURABLE_KEYS` and `EPHEMERAL_KEYS` frozensets in `config.py` determine which file a field reads from and writes to. `read_field()`, `write_field()`, and `write_fields()` handle the routing transparently.

The `config_path` parameter throughout the API became `config_dir` — callers pass a directory, and the read/write helpers resolve to the correct file within it.

### Why Two Files, Not Gitignore Patterns

A single file that is partially tracked requires `.gitignore` gymnastics or `git update-index --assume-unchanged`, both of which are fragile and confusing. Two files with clear ownership (tracked vs gitignored) is the standard pattern used by `.envrc` (tracked) + `.envrc.local` (gitignored).

### Migration

Auto-migration from `.vox/config.md` was handled by `vox install` and `vox daemon install` in v4.6.0. The v4.7.5 release removed `.vox/` entirely — no legacy fallback reads.

## DES-037: Remote voxd Connectivity via Env Vars

**Status:** SETTLED

### Problem

voxd binds to `127.0.0.1` and clients discover port/token from local files. Users who SSH from machine A (with speakers) to machine B (headless server) cannot hear audio — synthesis and playback both happen on B, which has no audio device. SSH reverse tunnels proved the protocol works remotely, but required manual file creation on B and stopping B's voxd to avoid port collisions.

### Rejected Alternatives

1. **SSH tunnel only (no code changes)** — works but fragile: requires manual `serve.port`/`serve.token` file creation, port collision if B runs its own voxd, tunnel dies with the session.
2. **mcp-proxy bridging** — the pattern lux uses for remote display. Vox's remote need is at the `VoxClient → voxd` layer, not the `Claude Code → vox mcp` MCP transport. Wrong connection to configure.
3. **Full TLS on voxd** — overkill for audio playback. Token auth is sufficient; SSH tunnel covers untrusted networks.

### Decision

Four env vars — three client-side, one server-side:

- `VOXD_HOST` (client): WebSocket host, default `127.0.0.1`
- `VOXD_PORT` (client): WebSocket port, default from `serve.port` file
- `VOXD_TOKEN` (client): auth token, default from `serve.token` file
- `VOXD_BIND` (server): bind address via `typer.Option(envvar="VOXD_BIND")`, default `127.0.0.1`

Resolution: explicit arg > env var > file > default. Two deployment models: direct network (same LAN) and SSH tunnel (different networks). Token auth is the security boundary. Access logs redact tokens. Users configure via `.envrc`. See `docs/guide-remote-setup.md` for the setup guide.

## DES-038: LaunchAgent over LaunchDaemon — Eliminate macOS Background Throttling

**Date:** 2026-05-27
**Status:** SETTLED
**Topic:** Move voxd from `/Library/LaunchDaemons/` to `~/Library/LaunchAgents/`

### Problem

macOS throttles LaunchDaemon processes (CPU QoS demotion, I/O deprioritization, thermal back-pressure). voxd synthesis measured 7x slower via LaunchDaemon vs manual launch: 17.4s vs 2.4s for a 38-character text. Every operation was uniformly slower — ElevenLabs API (4x), ffmpeg/pydub (19x), provider construction (11x). Texts over ~300 characters exceeded the 30-second client timeout.

### Decision

Move the plist from `/Library/LaunchDaemons/com.punt-labs.voxd.plist` (system domain, root-owned) to `~/Library/LaunchAgents/com.punt-labs.voxd.plist` (user domain, user-owned). LaunchAgents run at user QoS without background throttling.

**Plist changes**: remove `UserName` (invalid for LaunchAgents), add `ProcessType=Interactive` (prevents App Nap throttling on the windowless daemon). Use `launchctl bootstrap`/`bootout` (modern syntax) instead of deprecated `load`/`unload`.

**Fresh install**: no sudo. `mkdir -p ~/Library/LaunchAgents`, write plist, `ensure_port_free()`, `launchctl bootstrap gui/<uid> <plist>`, `launchctl kickstart`.

**Migration** (old LaunchDaemon exists): originally shipped as an automated path — write new plist, `sudo launchctl unload -w` the old system-domain plist, `ensure_port_free()`, `launchctl bootstrap` the new LaunchAgent, verify health, `sudo rm` old plist. **Removed 2026-07-01 — see Amendment below.**

### Why Not Keep the LaunchDaemon

| Alternative | Rejected |
|---|---|
| `ProcessType=Interactive` on LaunchDaemon | Undocumented for daemons |
| `Nice=-5` | Root-only, CPU-only, not I/O |
| Raise client timeout to 120s | Masks symptom, users still wait 17s |
| `bootstrap gui/<uid>` with LaunchDaemon plist | Mixing domains is unsupported |

### Supersedes

DES-028 §Service install (macOS): path changed from `/Library/LaunchDaemons/` to `~/Library/LaunchAgents/`.
DES-029 §macOS sudo calls: reduced from 4 steady-state to 0. (The 2 migration-only calls that briefly remained were removed — see Amendment.)

### Amendment (2026-07-01): migration path removed

The automated LaunchDaemon→LaunchAgent migration described above was removed before it ever shipped in a release (it lived only in `[Unreleased]`). Rationale:

- **Only ~3 total installs exist**, all on team machines. A one-time manual cleanup is cheaper than carrying, testing, and maintaining a one-shot migration path that becomes dead code the moment those machines are migrated.
- **It's a backwards-compat shim** — `PL-PP-1` forbids these ("if something is removed, it is removed completely").
- **It carried a live defect**: the final `sudo rm` of the old plist used `check=True`, so any sudo auth failure (no tty, wrong password, cancel) aborted `vox daemon install` with a traceback and exit 1 — even though the new LaunchAgent was already installed, booted, and health-checked. Every other step in the migration tolerated failure with `check=False`; only the least-important cleanup step hard-failed. Removing the path deletes the defect instead of fixing an edge case in dead code.

**Consequence:** `_install_darwin()` now runs the clean path unconditionally (stop → `ensure_port_free` → install → bootstrap → kickstart) with **zero sudo on macOS for both install and uninstall**. The `check_stale_launch_daemon()` doctor check (which only nagged users to migrate) and the `_OLD_LAUNCHD_PLIST` constant are gone. The pre-release machines that still carry the old system plist are cleaned up once, by hand:

```bash
sudo launchctl bootout system /Library/LaunchDaemons/com.punt-labs.voxd.plist 2>/dev/null
sudo rm -f /Library/LaunchDaemons/com.punt-labs.voxd.plist
```

Closes vox-zt3r. Shipped in v4.9.0.

## DES-039: Self-Driving Playlist — Eager Background Fill, Auto-Advance, Prefetch

**Date:** 2026-07-04
**Status:** PARTIALLY SUPERSEDED by DES-041 (rebuilt on the Program model; the eager-fill/auto-advance UX carried forward)
**Ticket:** vox-1rxb (rebuild of bas7 / #291)

> **Partially superseded (DES-041):** the filename-pattern pool, `PoolFiller`, and 29-method `MusicScheduler` are replaced by the persisted, ownership-free Program model. The *UX* this ADR locked — eager background fill to a full pool, auto-advance on track-end, zero-credit rotation — carried forward intact.

### Problem

bas7 (#291) shipped the wrong music UX. On track-end, `MusicLoop` respawns the
*same* file (`loop.py`: "Subprocess ended naturally — Return the same
current_track so the caller respawns it"), and the pool only grows when the user
runs `/music next`. Result: `/music on` plays one track that loops forever.
Confirmed by smoke test — pool stuck at 1, same track repeating. The subsystem
was built around the manual-skip path; the unattended listening experience was
never validated.

The root cause is a conflation of two concerns in one `gen_task`: the loop used a
single generation task to *both* prepare the next handoff track *and* (never)
grow the pool. Generation only fired on a `changed` signal, so nothing ran
"forward" of playback. There was no continuous supply and no auto-advance.

### Target behavior (operator-locked)

Put music on and forget it:

1. `/music on` (or a vibe change) generates track #1 and plays it the instant it
   is ready.
2. Immediately, the remaining tracks for that `(vibe, style)` pool generate in
   the **background, one at a time (sequential)**, until the pool reaches
   `POOL_SIZE` (12).
3. Playback **auto-advances**: when a track ends the next one plays with no
   command. Because background fill runs far ahead of ~3-min playback, the next
   track is already on disk — prefetch is a *consequence*, not separate
   machinery. Only while just track #1 exists does it loop #1 until #2 lands,
   then advance.
4. Once the pool has 12, generation stops and playback **auto-rotates** (shuffle,
   never the just-played track) among the 12 forever, at zero credits.
5. A vibe/style change **finishes the current song**, then switches to the new
   pool: resume background fill if that pool has < 12, else rotate.

### State / flow model

Four states own the daemon-wide music subsystem. The state is a derived view of
`(mode, pool-on-disk-count, fill-task-alive)`, not a stored enum to keep in sync.

```text
                turn_on / vibe-change (empty pool)
   ┌────────┐ ─────────────────────────────────────▶ ┌──────────────────┐
   │  off   │                                          │ generating-first │
   │ (idle) │ ◀──────────── turn_off ───────────────── │ (await track #1) │
   └────────┘                                          └──────────────────┘
       ▲  ▲   turn_on / restart (1..11 on disk)                 │ #1 ready
       │  │ ──────────────────────────────┐                     ▼
       │  │                                ▼             ┌──────────────────┐
       │  └───── turn_off ──────────  ┌─────────────────▶│  playing+filling │
       │                              │  track-end:      │  (pool < 12)     │
       │                              │  advance(pick_next)└─────────────────┘
       │   turn_on / restart (≥12)    │                     │ fill reaches 12
       │ ─────────────────────────────┤                     ▼
       │                              │             ┌──────────────────┐
       └───────── turn_off ────────── └─────────────│  full / rotating │
                                        track-end:  │  (no fill task)  │
                                        rotate       └──────────────────┘
```

- **off / idle** — `mode == "off"`, no player subprocess, no fill task.
- **generating-first** — `mode == "on"`, pool empty, fill task producing #1, no
  playback yet. The handler returns `"generating"` immediately; generation is off
  the handler's critical path.
- **playing+filling** — a track is playing and the fill task is alive
  (`pool < 12`). Track-end **advances** by selecting from the growing on-disk
  pool.
- **full / rotating** — a track is playing, the fill task has exited
  (`pool ≥ 12`). Track-end **rotates** (shuffle-avoid-last) with zero generation.

**Auto-advance on track-end.** The player subprocess ending is the trigger. The
loop asks the scheduler for the next track — a pure selection over the current
on-disk pool: `TrackPool.from_paths(gen.tracks_for(prefix)).pick_next(last)`. On a
one-element pool `pick_next` returns that same element (loops #1); once fill has
landed #2 it returns a different element (auto-advance); on a full pool it rotates
avoiding the just-played track. The "loop-#1-until-#2-lands" edge is not special
cased — it falls out of `pick_next`.

**Prefetch readiness** is therefore implicit: advance always reads the *current*
on-disk pool. If fill kept ahead (it always does at 3 min/track vs seconds/gen),
a fresh track is present. Readiness reduces to "does `pick_next(last) != last`" —
a consequence of the fill running forward, with no separate prefetch state or
task.

**The cancellable sequential background-fill task.** A new `PoolFiller` owns
exactly one `asyncio.Task`. Its body is `while not pool.is_full: await
generate_one()` — sequential by construction. It is retargeted or cancelled
through two methods:

- `ensure_running(vibe, style)` — if a task is alive for a *different* pool,
  cancel it and (if that pool is `< 12`) spawn a fresh one; if alive for the same
  pool, leave it; if the pool is already full, no-op.
- `cancel()` — cancel the task, awaiting its `CancelledError`, leaving no
  orphaned generation.

The **exactly-one-active-fill** invariant is structural: the class holds at most
one live task and always cancels before spawning.

**Vibe/style change** (finish current song, then switch): `update_vibe`
*immediately* retargets the fill — `PoolFiller.ensure_running(new pool)` cancels
the old pool's fill and starts the new one (bounds credit spend and gives the new
pool a head start) — and marks a pending playback switch. It does **not** kill the
current player. When the current song ends naturally, the loop switches playback
to the new pool (select from disk, or await #1 if the new pool is empty). This
replaces the mid-generation gapless handoff of DES-033 (see Supersedes).

**Restart** (`turn_on` reads the pool from disk): the on-disk count decides the
entry state directly. `≥ 12` → full/rotating, no generation. `1..11` → play a
pool member now, `ensure_running` resumes fill from the current count. `0` →
generating-first.

**`/music off`** — `turn_off` calls `PoolFiller.cancel()` *and* kills the player
in the same synchronous method: no orphaned generation, playback stopped, state
back to idle.

**`/music next`** (manual skip, unchanged role) — advance *now*: kill the player,
select the next track, play it. **`/music play <name>`** (named replay,
unchanged) — play the named track, retarget the pool/fill to that track's pool.

### Invariants preserved (each cited from the contract)

1. **Daemon/client boundary — no business logic in the client layer.** All new
   logic (`PoolFiller`, `select_next_track`, advance-on-end) lives under
   `voxd/music/`. `client.py` is untouched; handlers stay thin parse-and-delegate
   shells with unchanged signatures.
2. **`/music next` stays an optional manual skip; `/music play <name>` stays
   named replay; `/music off` cancels the fill task AND stops playback; gapless
   handoff preserved.** Skip/play/off map to the transitions above. `off` cancels
   fill synchronously. Handoff is now *near-instant* because the next track is
   already on disk — the loop kills the old player and spawns the next
   prefetched file with no generation wait (true zero-gap crossfade remains out
   of scope, as in bas7).
3. **Cache key, `--name` replay path, and deterministic collision-free naming from
   bas7 are unchanged.** Fill generates through the existing
   `TrackGenerator.generate(vibe, style, "")` → `auto_track_name` path; the named
   replay path (`find_track` → replay) is untouched. DES-035 stands.
4. **Reuse `TrackPool` (`is_full`, `pick_next`), `TrackGenerator`, the
   generate-vs-rotate decision.** `pick_next` *is* advance and rotate; `is_full`
   *is* the fill stop condition; the generator is reused verbatim. The
   generate-vs-rotate decision is now *split by owner*: rotate/advance = the loop
   via `select_next_track` (never generates); generate = `PoolFiller` (never
   plays).
5. **No `print()` in daemon code; logs to stderr only.** `PoolFiller` and the
   reduced loop log via `logging.getLogger(__name__)`.

### Rejected alternatives

1. **Prefetch-one-ahead vs eager-fill-all.** Prefetch-one-ahead generates only
   the single next track just-in-time before the current ends. Rejected: it never
   builds a reusable pool, so *every* advance costs credits forever; it couples
   playback duration to generation latency, so a slow generation produces a gap;
   and target behavior #4 explicitly wants zero-credit rotation over a filled
   pool. **Eager-fill-all** builds the 12-track pool once, then rotates free
   forever, and the "prefetched" next track is a side effect of the pool being
   ahead.
2. **Concurrent vs sequential fill.** Concurrent fires all 11 remaining
   generations at once. Rejected (operator-locked): it hits the ElevenLabs
   rate-limits bas7 already tripped; playback at ~3 min/track means one-at-a-time
   stays far ahead regardless; sequential bounds in-flight credit spend and is
   trivially cancellable at a generation boundary. **Sequential** wins on every
   axis here.
3. **Auto-advance in the loop vs a scheduler callback.** A scheduler callback
   would register an on-track-end handler that the subprocess watcher invokes.
   Rejected: the callback needs loop context (kill proc, spawn next), so it pulls
   playback wiring back into the scheduler; and bas7's test failure was precisely
   tests hitting the scheduler directly while the real loop looped one file —
   putting advance behind a scheduler method re-opens that trap. **Advance lives
   in the loop's proc-end branch** and calls the scheduler only for the *pure*
   `select_next_track` decision. Tests drive the loop and observe a real second
   subprocess spawned for a *different* file.
4. **`PoolFiller` owned by the loop vs by the scheduler.** Loop-owned leaves a
   window where the fill task generates one more track after `/music off` before
   the loop notices. **Scheduler-owned** lets `turn_off`/`update_vibe`
   cancel/retarget the fill synchronously — satisfying "off cancels the fill task
   (no orphaned generation)" and "a vibe change cancels the old fill and starts
   the new pool's fill" as locked. The scheduler *delegates* to `PoolFiller`
   (one-line calls); it does not implement the fill loop, so cohesion holds. The
   loop talks only to the scheduler, which is the facade over
   `(generator, pool, filler)`.

### Supersedes

**DES-033 (Gapless Music Handoff on Vibe Change)** — the mid-generation
concurrent-handoff model is replaced. A vibe change no longer loops the old track
while generating the new one; it *finishes the current song* (operator-locked),
having already retargeted the background fill so the new pool is ready. The
`music_changed`-race-plus-gen-task machinery that DES-033 introduced is removed:
the next track is prefetched by the fill task, so there is nothing to wait for at
the handoff. Gapless-ness now comes from the pool being ahead, not from looping
during generation.

### Amendment A (operator, 2026-07-04): disk access behind an injected `TrackStore` protocol

**Status:** SETTLED (operator directive during design review).

The music subsystem must not hard-code filesystem access. All track storage and
retrieval — pool enumeration (`tracks_for`), full listing, find-by-name,
the existence checks the deterministic naming counter relies on, and the write
target for a newly generated track — go through a **`TrackStore` protocol**
(a structural interface, PY-TS-6 / PY-IC-9), **injected** into the components
that need it. Domain code depends on the protocol, never on `Path.glob` /
`pathlib` directly.

- **`FilesystemTrackStore`** is the production implementation; the glob/dir logic
  currently inside `TrackGenerator` moves behind it. The daemon wires it.
- **Injection**: `TrackGenerator` (and, through the scheduler, `PoolFiller`)
  receive the store via their constructor. `daemon.py` constructs the
  `FilesystemTrackStore` and injects it.
- **Tests use an in-memory fake store** — pool-enumeration, selection, fill, and
  restart-from-count tests run with no `tmp_path`, no filesystem, no ffmpeg
  round-trip. (The one place a real MP3 is produced — the provider write path —
  still uses valid silent-MP3 bytes per `TESTING.md`; but the *domain* tests
  that made bas7's suite slow and filesystem-coupled now inject a fake.)
- **Write-set delta**: add the `TrackStore` protocol (in `types.py` per PY-IC-9)
  and a `FilesystemTrackStore` implementation module; inject it through
  `generator.py` → `scheduler.py`/`filler.py` → `daemon.py`. The exact protocol
  surface (method signatures) is settled in implementation; the *contract* — an
  injected, mockable, multi-implementation interface for all disk access — is
  locked here.

**Rationale.** Testability (mock the store: deterministic and fast, and the fill
/ restart / selection paths become unit-testable with zero filesystem), swappable
implementations (the operator's explicit requirement — e.g. an in-memory or
remote store later), and correct dependency direction (PY-IC-8: the domain
depends inward on a protocol, not outward on the filesystem). This also directly
serves the two-goals-together bar: the seam that makes disk access mockable is
the same seam that raises cohesion and testability on `generator.py` and the new
`filler.py`.

---

## DES-040: Daemon Failures Are Client-Observable Through the API, Not Logs

**Date:** 2026-07-05
**Status:** ACCEPTED (implementation tracked in vox-ig52)
**Topic:** How `voxd` surfaces background-operation failures to clients

### Problem

Background music generation can fail, and when it did the failure was invisible
to every client. Live 2026-07-05: a `/music on` prompt naming composers was
rejected by ElevenLabs (`400 bad_prompt`/ToS). `voxd` logged `Music could not
start; disabling` and raised; the user saw the panel say "generating…" then dead
air. The only record of the reason was in `voxd-stderr.log` — which no MCP
client, CLI caller, or user can read. A feature whose failure is invisible to its
clients is indistinguishable from a hang.

### Design

Every state and failure a client cares about MUST be observable through the
client interface — the MCP tool return value and the `status` tool — never only
in the daemon log. The log is an operator debugging aid, not a client interface.
For music: `status` carries a `music_state`
(`off | generating | playing | rotating | retrying | failed`) plus a
`music_last_error` with an actionable reason (for `bad_prompt`, include the
provider's suggested rewrite so a calling agent can self-correct); permanent
errors go to `failed`, transient ones to `retrying` with bounded backoff while
the existing pool keeps playing. The silent disable is removed.

### Why This Design

This is the daemon-side corollary of the org's Phase-3 verification rule
("observe via the project's introspection APIs"): the introspection surface
(`status`) has to actually carry the failure. It also forces honest
verification — you confirm a feature works by driving it and reading `status`,
not by grepping a log. Operator, 2026-07-05: "Reading logs is not a strategy for
our software clients."

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|-----------------|
| Log-only (status quo) | Invisible to clients — the failure above |
| Silent disable + retry forever | Hides permanent errors (`bad_prompt`, auth) that never succeed; still tells the client nothing |
| Push notification only, no queryable state | A client that missed the event can't learn current state; the queryable field is the contract, a notification is an optional nicety |

Full spec, error taxonomy, and z-spec state machine: `docs/vox-ig52-music-resilience.md`.

---

## DES-041: Audio Programs — Ownership-Free, Persisted Program Model (Phase 1)

**Date:** 2026-07-07
**Status:** SETTLED
**Topic:** Rebuilding background music as a first-class, persisted, ownership-free Program

### Decision

Background music is rebuilt on a **Program** model — a named, persisted pool of parts plus a manifest, driven by an explicit state machine — replacing the filename-pattern "pool" and the 29-method `MusicScheduler` god-facade. Phase 1 realizes the `playlist` format; `podcast`/`audiobook` are named in the type vocabulary but not built. Design of record: `docs/audio-programs-phase1-design.md`; formal contract: `docs/audio-programs.tex` (a fuzz-clean Z model, 16 state invariants validated by construction).

Key decisions:

- **Ownership removed.** `voxd`'s Program state is machine-universal — any client (MCP session or CLI, from any process) drives any command. The prior session-ownership gate (source of the vox-73m5 stale-vibe bug) is gone. A vibe change is a deliberate music command, never a silent side effect of setting the session mood.
- **Single-writer `ControlChannel`.** Every mutation is a typed `ControlSignal` posted to one FIFO drained by one consumer (O2), so no handler races the Program. A benign lost race (`GuardViolationError`) logs at INFO; a corrupt successor is a bug at ERROR.
- **Persisted + replayable.** Pools save to `~/Music/vox/<name>/` (named by `--name`, else the style; **no `programs/` segment**) with a `manifest.json` and **ID3 tags** on every track, so `/music play <name>` / `list` / `next` / `loop` / `playlist:N` replay from CLI or MCP at zero credits.
- **No migration.** The flat `~/Music/vox/tracks/` layout is not migrated; the `vox music migrate` command and start-up hint from an earlier draft were struck under the org no-migration rule (no installed user base). Forward integration only — `voxd/music/` deleted in the same PRs.

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|-----------------|
| Keep the filename-pattern pool | "It's a naming pattern, not a list" (vox-us4g) — no manifest, no replay, no status |
| Keep session ownership | Complex, bug-ridden (vox-73m5 stale vibe), unusable across sessions (operator, 2026-07-05) |
| Ship a `vox music migrate` bridge | No user base to migrate; a migration bridge is complexity for zero reason (org no-migration rule) |
| Per-vibe pools now | Deferred — Phase 1 keys pools on style (vibe flavors the agent's prompts only); per-vibe identity is `vox-q7vh` (direction A) |

### Shipped deviations from the design

Recorded in `docs/design-review-phase1.md`: the *format-general* claim is partly aspirational (`Program.rotate` raises on `COMPLETE`, no terminal `Mode`, `Subject` is concretely `PlaylistSubject`, so Phases 2–3 build those seams rather than merely supply them); `subject.vibe` records the style, not the session vibe (`vox-q7vh`); the per-command `applied/rejected` wire field is unreachable in Phase 1 (handlers ack at enqueue).

Closes vox-oayr.

---

## DES-042: The Mic Metaphor — Why the Speak Tool Is `unmute`, Not `say`

**Date:** 2026-07-11
**Status:** SETTLED
**Topic:** The playful mic-metaphor UX, and why the MCP speak tool's name deliberately diverges from the CLI's `vox say`

### Decision

The MCP tool the **agent** calls to speak is named **`unmute`**, and it stays that way. This is a deliberate, playful UX choice, not an inconsistency to be normalized against the CLI.

The tool name renders in the Claude Code tool-result panel (the `♪` line, DES-008) in the moment just before the agent talks. `unmute` reads as *the agent turning mute off on its own mic* — breaking its silence to say something. That framing is the point. It gives the surface the agent drives most a small, coherent piece of character.

The metaphor is a family, not a one-off:

- **`unmute`** — the agent flips its mic on to speak.
- **`/mute`** — mic off; chimes only (the agent goes quiet, DES-004).
- **`who`** — who's at the mic? (the voice roster).
- **`♪`** panel glyph (DES-008) and the voice-vocabulary tool names — `speak`/`chorus`/`duet`/`ensemble` (DES-007).

### Why It Deliberately Diverges From `vox say`

The CLI and the MCP surface have **different actors**, and their verbs correctly reflect that:

| Surface | Actor | Verb | Reads as |
|---------|-------|------|----------|
| CLI | a **human** at a shell | `vox say "hello"` | "say this for me" |
| MCP | the **agent**, on its own initiative | `unmute` tool | "the agent unmutes its mic" |

A user never types `/vox:say "hello"` — the agent speaking arbitrary text on command is not a user action. Users type slash commands (`/vox`, `/vox:mute`, `/vox:unmute`, `/vox:vibe`, `/vox:music`, `/vox:recap`); `/vox:unmute` enables voice mode / sets the session voice, which lives inside the same mic metaphor. `say` belongs to the human/CLI surface; `unmute` belongs to the agent/MCP surface. Unifying them (renaming the tool to `say` for "cross-surface consistency") would flatten an intentional distinction between two different actors and delete the charm — a regression, not a cleanup.

This is distinct from the prfaq's *"Won't Do: agent personality voices"* boundary. That exclusion is about the *audio* not role-playing a character (the voice sounds tired after failures for *signal*, not performance). The mic metaphor is light naming texture in the tool surface — playful, not a persona.

### Rule

**Do not re-flag the `unmute`-vs-`say` divergence as an inconsistency to fix.** It is a settled positioning choice: two surfaces, two actors, two correct verbs. The mic metaphor (`unmute`/`mute`/`who`/`♪`) is deliberate and load-bearing UX charm. New evidence of user confusion — not an agent's tidiness instinct — is the only thing that reopens this.

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|-----------------|
| Rename the MCP `unmute` tool → `say` to match the CLI | Flattens the human-vs-agent actor distinction; the agent "unmuting its mic" is intentional character the panel shows before every utterance; deletes the metaphor |
| Rename the `speak` toggle / consolidate `notify`+`speak` | No surface to match against; the user-facing `/mute`+`/unmute` slashes are shipped, documented product (prfaq FAQ, Feature F4); this is invented scope |
| Leave the intent undocumented | Already caused a false "bug" report (vox-yn8u round, 2026-07-11) where the divergence was misread as an inconsistency — this ADR is the fix |

---

## DES-043: Auto-Vibe Is Agent-Driven, Not Deterministically Classified

**Date:** 2026-07-13
**Status:** SETTLED (supersedes the vibe-signal machinery of DES-018)
**Topic:** How `/vibe auto` derives the session mood

### Decision

Auto-vibe sets the TTS mood from the **conversation, judged by the main agent**, not from any deterministic per-command signal. A non-blocking `UserPromptSubmit` hook (`plugin/hooks/vibe-nudge.sh` → `vox hook vibe-nudge`) injects a soft `additionalContext` reminder every Nth user prompt (N=5), **only when `vibe_mode == auto`**, nudging the agent to glance at the session and set the vibe via the `vibe` tool if the mood has shifted — `[happy]` when flowing, `[focused]`/`[frustrated]`/`[weary]` when stuck, `[relieved]` after a fix. The cadence counter (`vibe_nudge_turns`) lives in the ephemeral `vox.local.md`; a `/vibe` mode change and session end reset it.

Design of record: `docs/vibe-agent-driven.md`. **No formal model:** the state that justified the interim Z model (an exit-code window/mood accumulator) is deleted; the replacement is a stateless nudge plus a bounded mod-N counter, below the formal-modeling trigger.

### Why

Two prior deterministic mechanisms failed. (1) The output-pattern classifier grepped command *output* for pytest/ruff/git tokens — **narrow** (only this repo's toolchain), **asymmetric** (a clean exit with no recognized token produced no signal, so successes went uncounted and the mood skewed frustrated everywhere else), and **fragile** (`vox-p0u6` was the acute symptom). (2) An interim exit-code accumulator tried to derive the mood from each Bash command's exit code read from the `PostToolUse` hook — but **that signal does not exist**: Claude Code does not expose the exit code to `PostToolUse` hooks (the `tool_response` carries only `stdout`/`stderr`/`interrupted`/`isImage`/`noOutputExpected`, and the result is finalized *after* the hook runs), confirmed from the Claude Code docs and a live payload capture, so the accumulator recorded nothing. The agent, which sees the whole conversation, holds the success/failure context no per-command hook ever could. Validated by a live spike.

### Consequences

- The exit-code accumulator (`vibe_window`, `vibe_mood`), the `PostToolUse` Bash hook and `BashPayload`, the `vibe_signals` config field, and the interim design doc + Z model (`docs/vibe-exit-code*`) are deleted (forward integration, no shims). `vibe_signals` is replaced by the `vibe_nudge_turns` cadence counter.
- The transcript watcher and the dead mood-pitch chime machinery stay deleted; notification chimes are two flat tones. The mood colors the **spoken voice** (ElevenLabs `vibe_tags`), not chimes.
- The nudge fires only every Nth prompt, so a mood shift inside a short window registers on the next nudge, not instantly — acceptable for ambient TTS mood, and the agent may set the vibe at any time regardless.

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|-----------------|
| Exit-code per command (interim) | The signal does not exist — `PostToolUse` hooks never see the Bash exit code (see Why). |
| Output-pattern classification (DES-018-era) | Narrow, asymmetric, fragile. |
| LLM call inside the hook | The hook runs outside the model context and must be near-instant; an in-hook LLM call is slow, costly, and blind to the conversation the reminder exists to leverage. |
| Nudge every prompt | Nags. The cadence counter throttles to every Nth prompt. |

Closes vox-ek1m.

## DES-044: The Music Panel Speaks Like a DJ — Server-Authored, Not Hook-Composed

**Date:** 2026-07-15
**Status:** SETTLED
**Topic:** Where the fun in the `♪` music panel line lives

### Decision

The **server authors** the music panel line (`updatedMCPToolOutput`) with DJ-booth personality, drawing a randomized phrase from a curated pool and filling in the real `style`/`name`. The `suppress-output.sh` hook stays a **dumb echo** of the tool's `.message` (first line, ≤ 80 cols). The dead DJ phrase pools in the hook — which keyed off `.status`/`.style`/`.name` fields the music tools never return, so they never fired — are **deleted** (forward integration, no dead code).

Phrase pools (curated here; the implementation lifts them into the phrase registry, `quips.py` or a sibling, as immutable tuples and selects one per call). Each is `♪`-prefixed at emit and kept short so the prefixed line stays ≤ 80 cols:

- **Music on / generating, with `{style}`:** "dropping a {style} beat" · "{style} in the booth" · "cueing up {style}" · "{style} on the decks" · "spinning up some {style}" · "{style} — beat incoming"
- **Music on / generating, no style:** "beat incoming" · "stepping up to the decks" · "warming up the decks" · "cueing the first track"
- **Music off / stopped:** "fading out" · "that's a wrap" · "decks off" · "last call" · "killing the lights"
- **Replay (`music_play`) with `{name}`:** "now spinning: {name}" · "{name} on the decks" · "{name} on repeat" · "pulling {name} from the crate" · "{name} — encore"
- **Replay, no name (radio):** "back to the crate" · "shuffling the crate" · "radio mode — full crate"
- **Skip (`music_next`):** "mixing the next one in" · "next track loading" · "cueing the next" · "on to the next"

### Why

vox-lf6b's review discovered the hook's DJ pools were unreachable dead code: `music`/`music_play`/`music_list` return only `{message, applied}` / `{message, programs}` — no `.status`/`.style`/`.name` for the hook to branch on — so the panel silently fell back to a generic "♪ music updated", and vox-lf6b corrected the hook to echo the server's plain `.message`. The fun the operator wanted ("fun is a feature") was never shipping. The tool is the one place that holds the real action + style + name, so it is the correct author of a flavored line; the hook is a display surface, not a content generator. This also keeps the phrase logic in Python — testable (pool membership, injected selection) — instead of in bash.

### Consequences

- The hook's `music`/`music_play`/`music_next`/`music_list` DJ phrase pools and their `.status`/`.style`/`.name`/`.tracks` branching are deleted; the hook echoes `.message` and derives the `music_list` count from `.programs` (already true after vox-lf6b).
- The panel line loses the informative "generating a trance track for your `<mood>`" wording in favor of DJ flavor; the agent does not need that wording (control tools carry the stop-narration directive in `additionalContext`, per vox-lf6b).
- Selection is randomized per call; tests assert pool membership and (via an injected chooser) determinism, never a live RNG assertion.
- No genre-alien constraint applies — these are panel *action* phrases, not ElevenLabs music prompts, so the artist/copyright rule of DES-039-era music generation does not govern them.

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|-----------------|
| Hook composes from structured fields | Requires the tools to return `style`/`name`/`status`, widening the tool contract, and keeps hard-to-test phrase logic in bash. The tool already holds the context. |
| Keep the plain server `.message` | Ships correctness but not the fun; the operator explicitly wants the DJ personality. |
| Curate phrases per genre | The panel line is an *action* confirmation, not genre-specific; genre variety belongs in music *generation*, not the panel. |

Closes vox-1jke.

## DES-045: A Mood Change Re-Pools the Music — Hint-Based, Not Coupled

**Date:** 2026-07-16
**Status:** SETTLED
**Topic:** How a vibe/mood change drives a music re-pool

### Decision

When the vibe-set tool is called (via `/vibe` **or** the agent's own auto-vibe assessment — no distinction) **and a music Program is playing**, the music re-pools to the `(new_vibe, style)` pool: an existing pool rotates in for free, a new pool generates. When music is **off**, the vibe change updates the speaking mood only — a music no-op. No confirmation, no credit guard.

Crucially, **`vibe()` does not drive playback.** It stays a pure voice-mood tool. It reads music status **read-only** and enriches its *return* with a `music` state object plus an imperative `music_hint` directive — e.g. *"Music is playing (style=flamenco). Author 12 rich flamenco×`<mood>` prompts and call `music(mode=on, style=flamenco, …)`. Do it now."* The **agent** acts on that hint: it authors the 12 rich `(mood × style)` prompts (the mood *colors* the genre) and calls the existing `music` tool, which performs the re-pool via the unchanged `VibeStyleChange` transition. The hint fires only on genuinely-audible modes (`PLAYING_FILLING`/`PLAYING_ROTATING`), never on `FAILED`/`RETRYING`/`OFF`.

### Why

Separation of concerns. `vibe()` is voice direction; driving playback from it couples a mood tool to the music state machine — a layering violation. The return-hint keeps the concerns clean: **vibe = mood, music = music, agent = orchestration.** The imperative directive in the return is the STOP_NARRATION-style device that makes the soft, agent-orchestrated path reliable, and it reuses authoring the agent already does on `/music on style`. Applying it uniformly to manual and auto vibe (rather than gating auto to free rotations) was the operator's call — simpler, and the credit spend is intended.

### Consequences

- `vibe()` gains a read-only music hint in its return and **never posts a switch/music signal** (asserted by test). The `music` tool's re-pool is unchanged — no daemon or state-machine change, so **no Z-model change** (the re-pool is the existing `VibeStyleChange`, still triggered only by the `music` tool).
- The authored style is tracked in a cohesive `MusicPreference` session register, maintained on **every** playback-changing path (`music on` adopts, `music_play` adopts or clears for a union radio, `music stop` clears) so the hint always names the genre actually playing.
- Reliability is **soft** (prompt-level — the agent must follow the hint). Mitigated by the imperative directive and made *provable* by the `[vibe-trace]` observability (DES-046).
- Reverses the prior "the session vibe is display/record state; a Program retune is a deliberate music command, never a side effect" decision (the `vibe()` comment), which is struck.

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|-----------------|
| Couple the re-pool inside `vibe()` | Layering violation — a voice-mood tool driving playback internals. The hint keeps concerns separate. |
| Credit guard / confirmation before generating | Operator overrode — the re-pool is the intended effect of a mood change; a confirmation is friction. |
| Flavorless daemon fallback prompt for a new pool | `"<style> music, <mood>. loopable"` is the homogenized tail `/music` forbids; routing the *mood-driven music* feature through it regresses genre fidelity precisely where the feature should shine. |
| Gate the coupling to manual `/vibe` only (auto stays a music no-op) | Operator: no distinction, simpler. Auto and manual both call the vibe tool; both re-pool. |

Closes vox-q1z4.

## DES-046: Prove Soft, Agent-Driven Mechanisms With a Structured Trace

**Date:** 2026-07-16
**Status:** SETTLED
**Topic:** How to verify a prompt-reliant (soft) mechanism actually fires

### Decision

Any **soft, agent-orchestrated** mechanism — one whose correctness depends on the LLM following a hint or nudge rather than a hard code path — MUST emit a stable, greppable structured trace (`[vibe-trace]`) at **each link** of the chain, so a human can *prove* the chain fired or catch a silent gap. For the two current soft mechanisms:

- **auto-vibe (DES-043):** nudge fired → a following vibe-set with `mode=auto`. A nudge with **no** following vibe-set = auto-vibe silently not firing.
- **vibe→music (DES-045):** a vibe-set with `music_playing=true` → a following `music` re-pool. A playing vibe-set with **no** re-pool = the agent dropped the follow-up.

Observability is a first-class deliverable of any such feature, not an afterthought.

### Why

Soft mechanisms cannot be guaranteed by unit tests — the LLM's follow-through is out-of-band from the code. The only way to know they work *in production* is an observable event trail. This generalizes the recurring lesson of this line of work (the `/music` narration that a markdown line failed to enforce; auto-vibe): soft agent behaviors are invisible — and therefore unfalsifiable — until you can `grep` for them.

### Consequences

- `[vibe-trace]` events at the nudge (`NudgeHook`), vibe-set (`VibeCommand`), and music (`server.music`) links, via `logger.info` (never `print`), pinned at a level that always reaches the log.
- Because the vibe/music logic runs **client-side** (the `mic` MCP server and hooks), the trace is written to a **persistent, append-only log file** at a known path under the vox state/log directory — shared by the MCP server and the hook subprocesses via multi-process-safe atomic appends — **not** voxd's `tts.log`. The `grep '[vibe-trace]'` proof recipe in `plugin/commands/vibe.md` targets that file. **(Amended 2026-07-16 — see below. The original decision routed the trace to stderr; that was wrong.)**
- Current state (the session vibe and the playing music style) is *also* surfaced through the `status` tool — the trace is the event-trail *proof over time*; `status` is the point-in-time *client-observable state*. Both, per "client-observable, not logs."

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|-----------------|
| No observability | The mechanism is unprovable — the operator made "prove whether it works" a core goal for both this feature and auto-vibe. |
| Log only in voxd | The vibe/music orchestration is client-side, not in the daemon; a daemon log would never see it. |
| `status`-only | A point-in-time query cannot prove a *sequence* of events fired across a session — only the trace can show "nudge → vibe-set → re-pool." |
| **Trace to stderr** (originally chosen) | **Wrong — reverted (vox-9po7).** The MCP host discards MCP-server and hook stderr; it is not persisted to any greppable file. A live smoke test on 4.12.2 confirmed `grep '[vibe-trace]'` finds nothing anywhere. "Log to stderr" was functionally "do not log." |

### Amendment — 2026-07-16 (vox-9po7): stderr sink was a mistake; trace goes to a persistent log file

The original Consequences routed `[vibe-trace]` to the MCP-server / hook **stderr**, assuming Claude Code captured it to a greppable log. It does not: the CLI's per-server MCP log holds only client-side "Calling MCP tool" wrapper lines, and the server/hook stderr is discarded. The 4.12.2 live smoke test found the feature itself works end-to-end (the `music_hint` round-trips through the API and the agent re-pools), but the DES-046 proof — a **core** operator goal — was unreachable: no `[vibe-trace]` line existed in any runtime file.

**Correction:** the trace is written to a **persistent, append-only log file** at a known path under the vox state/log directory, shared by both emitters (MCP server + hook subprocesses) via multi-process-safe atomic (`O_APPEND`, single-line) writes. The stderr emission is **deleted** (forward integration, PY-RF-6 — no dual-write). `plugin/commands/vibe.md` documents the real file path. The trace format is unchanged; only the sink moved. Root cause of the mistake: the decision assumed the host persisted stderr without verifying it against the running system — the "verify outputs, not just metrics" discipline applied to observability, not just features.

Closes vox-q1z4. Observability sink corrected under vox-9po7.

## DES-047: Fun Is a Feature — Entertainment Is In Scope, Not a "Won't Do"

**Date:** 2026-07-19
**Status:** SETTLED
**Topic:** Whether entertainment / personality / fun is a product goal or an excluded non-goal

### Decision

"Fun is a feature" is part of the punt-labs product **spirit** (the org/product ethos, not the ethos identity tool). vox is **partly** entertainment by design — not a purely utilitarian notification tool. The prior framing that walled entertainment off as out-of-scope is **struck**: the `prfaq.tex` "Won't Do: Agent personality voices" feature-appendix item and the matching "Not personality entertainment" FAQ bullet are **deleted** (operator ruling, 2026-07-19).

Two things are the deliberately-fun side of vox:

- **Agent-as-DJ (shipped).** The music panel's DJ-booth personality (DES-044) and the vibe-matched background-music pools are intentional entertainment — the agent plays DJ for you.
- **Codebase-aware podcast + audiobook programs (roadmap).** Upcoming audio-program formats (the DES-041 Program model, Phases 2–3) whose content is drawn from the codebase's own **domain**, the **technology** it uses, or **fiction inspired by** it — so a developer can step back, laugh, and not burn out.

**The "partly" is load-bearing.** The work/notification **voice's mood stays honest signal** — tired after failures is *signal*, not a performed theatrical persona (this is the narrow, still-true part of DES-042, which is **not** reversed). What is reversed is only the *broad* reading that vox avoids entertainment altogether: the DJ layer and the podcast/audiobook programs are in scope.

### Positioning weight

In `prfaq.tex`, fun is a **light-touch** benefit among several — **not** a lede/headline pillar (operator ruling). The top-line positioning stays utility-led (voice+audio layer; eyes-free progress tracking); the DJ line sits in the Solution + Shipped features, and podcast/audiobook sit under "Should Do / Next." Applied at prfaq v2.1 (current doc version v2.2).

### Relationship to DES-042

DES-042 (the mic metaphor; notification-voice mood = signal, not performance) **stands**. This ADR does not license the notification voice to role-play a character. It only removes the blanket "no entertainment" scope exclusion so the DJ + podcast/audiobook fun is admissible.

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|------------------|
| Keep "Not personality entertainment" as a Won't Do | Contradicts "fun is a feature" — treated a real product value (entertainment / anti-burnout) as out of scope |
| Rescope the exclusion narrowly instead of deleting it | Operator ruled delete outright; the only narrow truth (notification voice = signal) is already covered by DES-042 |
| Make fun a lede/headline pillar | Operator ruled light-touch — the positioning stays utility-led; fun is one benefit among several |

Relates to vox-iyqq (positioning) and the audio-programs epic (podcast/audiobook = Phases 2–3).

## DES-048: One vox.log — Every Process Appends Directly; the Ship Transport Is Deleted

**Date:** 2026-07-20
**Status:** SETTLED
**Topic:** Log transport for the unified `vox.log` (vox-2594, fixing the vox-fdmm defect)

### Decision

Every process — daemon and every client, including hooks that do **no** daemon work — appends its own records **directly** to one `vox.log` through the multi-writer-safe `O_APPEND` line writer (`AtomicAppendLog`), with rotation guarded by a `flock` shared/exclusive protocol on a stable lock file (DES-013 size-check-then-rename shape, modeled in `docs/vox-2594-log-rotation.tex`, fuzz-clean). The fdmm ship transport (`log_ship.py`, `log_flush.py`, `log_wire.py`, `voxd/log_sink.py`) and `vox-fallback.log` are deleted outright — no fallback file exists. The daemon logs synchronously on its event loop (one uncontended `flock` per record); a thread offload was explicitly ruled out as premature (operator-ratified 2026-07-20).

### Why

fdmm (v4.12.5) routed client records over the WebSocket a client opens *for its actual work*, with an `O_APPEND` "daemon-down" fallback. Two false premises: a no-daemon-work hook opens no WebSocket, and the fallback was not a daemon-down path — it was the *primary* path for the largest client class (skip-path hooks). Live measurement: `vox-fallback.log` 4.2 MB + rotations vs `vox.log` 404 KB. A transport that cannot carry the largest client class cannot deliver "one log." Direct append needs no transport at all: `O_APPEND` single-line writes are already atomic across writers; the only genuinely new safety problem is multi-writer rotation, closed by the `flock` protocol (LOCK_SH held across every `open→write→close`; LOCK_EX + size re-check to rotate — no write to a renamed file, at most one rotator, no lost lines).

### Consequences

- Invariants (tested by name): one `vox.log` for daemon + every client record; no daemon round-trip on any hook's logging hot path (DES-017); rotation safe under concurrent writers; 0600 on active file and backups (cn0p); no fallback/migration/shim; persistent-file observability (DES-046).
- The daemon is no longer the log owner; `voxd/daemon.py` drops the `log` frame route. Client lines are stamped `client.<role>.<module>` for grepping.
- A logging failure degrades to a `sys.__stderr__` note — never a crashed hook.

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|-----------------|
| A: no-daemon-work hooks open a brief connection to ship their line | Every fast hook pays a daemon round-trip (violates DES-017) and a daemon-down fallback is still required — the two-file split survives |
| C: clients append to a local spool the daemon drains into `vox.log` | Buys daemon-sole-writer, which `O_APPEND` atomicity already provides, at the cost of a drain loop, spool lifecycle, and event-to-visibility latency |
| Keep the fallback but make the split observable | The split *is* the defect — "one vox.log" was the shipped promise; observing the failure is not fixing it |
| Thread offload for the daemon's synchronous `emit` | Same order of cost as the previous in-loop `RotatingFileHandler`; offload now is speculative complexity — revisit only if live-verify shows loop stalls |

Closes vox-2594. Supersedes the fdmm transport recommendation (`docs/logging-proposal.md` rec 3); design of record: `docs/vox-2594-unified-log.md`.

## DES-049: The Daemon Is the Audio Host — Record-Store Containment, Play, and Fetch

**Date:** 2026-07-21
**Status:** SETTLED
**Topic:** Closing the `#351` arbitrary-write hole and making the remote record→play→retrieve loop coherent (vox-dvri, closing vox-zu39, vox-ovb7, vox-eoq9)

### Decision

The daemon owns audio files and playback; clients are thin controllers that
never dictate a daemon path and never play remote audio locally. Three linked
problems close as one change under the operator-ratified **pure model**:

- **Record captures to a daemon-owned store.** `vox record` takes no `-o` and
  sends no path. It synthesizes into `~/.punt-labs/vox/recordings` (`0700`)
  under a content-addressed name or an optional bare `--name`, and returns a
  **locator** (`RecordResult(id, name, store_path, byte_count, cached)`), never
  a client write path. Materializing a copy is a separate, explicit step,
  `vox fetch <id> -o <path>`.
- **Containment is the security primitive (P1, vox-zu39).** A new `RecordStore`
  owns the root and every path decision: it rejects a candidate that is
  absolute, separated (`/`, `\`), traversing (`..`), empty, or NUL-bearing
  before any filesystem touch, resolves it under the root, and verifies
  `resolved.is_relative_to(root)` **after** `.resolve()`. `resolve` (record
  naming) and `resolve_ref` (play/fetch) share one validator. The token
  authorizes audio operations, not filesystem writes as the daemon user.
- **Play routes through the daemon (vox-ovb7).** A `play` wire op resolves a
  store ref and enqueues it on the serialized `PlaybackQueue`, so audio plays on
  the host with speakers; the CLI still plays an existing local file client-side
  (loopback = the right machine).
- **Fetch retrieves (vox-eoq9).** A `fetch` wire op returns a store recording's
  bytes in one bounded frame; `vox fetch` always retrieves those bytes from the
  connected daemon — there is no local-copy shortcut, because a same-named local
  file cannot prove it is the store recording (identity, not existence). A
  recording larger than the frame limit is refused, not truncated.

### Why

`#351` moved record writes daemon-side but let a wire client choose the absolute
`output_dir`/`output_path`. Across the documented remote setup a compromised
machine B (holding A's token) could overwrite any file A's user can write —
likely RCE. The trust boundary is **not** the network interface (an SSH tunnel
makes a remote peer look loopback), so a "local fast-path" that trusts loopback
would hand the primitive straight back; peer address is never a security input.
Removing the client-path write primitive by construction — bare name + a
daemon-owned root with a post-`.resolve()` containment check — is the only sound
fix. The pure model (record captures, fetch materializes) keeps `record` a
single path-free verb and preserves `#351`'s byte-free hot path: bytes reappear
only on the opt-in, cold `fetch`.

### Consequences

- Invariants (tested by name): a wire absolute path / traversal / separator /
  empty / NUL name is rejected; no write escapes the root; the token grants no
  FS write; `play` routes through the daemon and a ref outside the root is
  refused; `fetch` delivers bytes and refuses an out-of-root or oversize ref;
  `record` returns a store locator, never a client path.
- Forward integration: the `#351` `output_dir`/`output_path` wire fields and
  `record_sink.py` are deleted, not bridged. `RecordStore` absorbs the atomic
  write with the containment check added.
- The record-store containment property is formally modeled in
  `docs/vox-dvri-record-store.tex` (`fuzz -t` clean): the invariant "every
  stored write stays within the root" lives in the `Store` schema predicate,
  `Place` preserves it, `Reject` covers the hostile inputs, `Play`/`Fetch`
  resolve only in-store refs.
- Out of scope (own quick fixes / follow-ups): daemon status via `client.health`
  (vox-4p5p), cache via daemon (vox-suvs), store retention/eviction, chunked
  streaming fetch for large remote recordings, remote arbitrary-local-file
  playback (a future `push` op).

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|------------------|
| `record -o` as client-side delivery (copy locally, fetch remotely) | The operator ruled the pure model: overloading `record` with a delivery path re-introduces a client path on the hot command and a local/remote branch the user must reason about. Separating capture (`record`) from materialization (`fetch`) keeps `record` path-free and puts the one copy-vs-wire decision behind `fetch`. |
| (b) Local fast-path: loopback keeps direct client-path writes, remote is sandboxed | Unsound by the guide's own topology — the SSH tunnel makes a remote B a loopback peer, so "local = trusted" hands the arbitrary-write primitive back to the tunnelled remote, and it keeps the dangerous write path alive as a security-critical branch. |
| (c) Always stream bytes back; client writes locally | Reverts `#351`: re-adds base64/bytes-over-wire on every record (the 1 MiB frame ceiling, the unbounded receive buffer) and abandons the daemon-audio-host model that `play <id>` needs. |
| Daemon echoes the client-requested absolute path | Echoing a client path is the exploit surface itself; the point is that the client never names a daemon path. |

Closes vox-zu39, vox-ovb7, vox-eoq9 (epic vox-dvri). Design of record:
`docs/vox-dvri-daemon-audio-host.md`; containment model:
`docs/vox-dvri-record-store.tex`.

## DES-050: One Verb Vocabulary for the Two Audio Stores (rec / music)

**Status:** accepted (vox-jei3). Design of record: `docs/rec-music-cli.md`;
models: `docs/vox-chunked-transfer.tex`, `docs/audio-programs.tex` (Catalog/System delta).

### Context

vox has two daemon-owned audio stores. Music albums already had a coherent
`vox music` Typer group (`list`/`play`/`next`/`status`); recordings did not —
they were three scattered top-level verbs (`record`/`play`/`fetch`) with gaps
(no `list`, no `remove`), an overloaded `play` (store id *or* local file, by a
filesystem probe), a leaked daemon path from `record`, and a stray `-o` that
let the client name an output path. The rough edges (a printed path that
`fetch` then rejects; no way to enumerate the store) were symptoms of the
recordings feature never getting the group treatment music had.

### Decision

Give recordings the same group shape and make **both** stores share one verb
vocabulary — `new` / `list` / `play <id>` / `get <id>` / `remove <id>` (music
also keeps `next` / `status`, being a running Program) — on the CLI **and** the
MCP surface at parity (one engine, thin clients, one code path).

- **`list`/`remove` spelled out**, not `ls`/`rm` — the other verbs are English words.
- **No `-o` anywhere; `new` prints the bare store id; `get` writes into the CWD
  under the store's own name** (rec = one file; music = a directory of the
  album's parts), refusing to clobber. The client never names a daemon path.
- **Chunked `get`.** The 700 000-byte single-frame fetch is replaced by a
  bounded, ordered, sha256-verified chunk stream landed atomically, so any-size
  recording or multi-MB album transfers in full — the limit was a bug to fix,
  not a constraint to design around.
- **`music new` is catalog authoring, distinct from the `music on` program.**
  It takes the finished ElevenLabs prompt verbatim (no LLM style→prompt
  expansion in the CLI/daemon — the invoker supplies what an agent would author
  in the MCP flow), generates one track into a fresh single-track catalog album,
  and parks it — the running Program's mode/pool/`lastError` are untouched, and a
  generation failure is reported to the caller, never promoted to a program-level
  `failed`. This is the crux of the Z-model delta: `new`/`remove` mutate the
  **Catalog**, not the resolved Program pool (finding #7 preserved).
- **`music play` keeps its tag radio** (`--style`/`--vibe`/`--name`) alongside
  the bare `<id>` positional — no shipped behavior removed.
- **`say` stays** as the ephemeral speak-now verb (synthesize + play, no store,
  no id); `rec new` is its durable counterpart.

### Forward integration (no shims)

`vox record`/`play`/`fetch`, the `-o` flag, `vox play <localfile>`,
`_emit_record_locator`, `_atomic_write_bytes`'s old form, and
`FETCH_FRAME_LIMIT_BYTES` are deleted outright — no aliases, no deprecation
hints (PY-RF-6; vox has no installed base to migrate). Local-file playback is
the OS tool's job.

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|------------------|
| Keep recordings as flat top-level verbs, just add `list`/`remove` | Leaves the incoherent shape (scattered verbs, overloaded `play`, leaked path); the group + shared vocabulary is what makes the two stores learnable as one. |
| `ls`/`rm` (terse) | `new`/`play`/`get`/`next`/`status` have no natural short forms, so `ls`/`rm` would be the only abbreviations — one register, spelled out. |
| Defer `music get` (tracks exceed the 700 KB fetch frame) | Shipping a `get` that refuses its main input is the rough edge we set out to remove; chunked transfer fixes the transport so `get` works for any size. |
| Lean MCP (CLI-only management) | The projection model (architecture.md) makes MCP a first-class surface for an agent-facing tool; the same capability is exposed on every surface with callers via one engine path. |

Closes vox-jei3.

## DES-051: One MCP Tool Per Audio Group — Subcommand as the First Argument

**Status:** accepted (Audio Programs Phase 1.5, vox-ys1p). Design of record:
`docs/audio-programs-music-tidy.md`.

### Context

DES-050 gave the CLI two parallel groups, `vox rec` and `vox music`, sharing one
verb vocabulary. The MCP surface reached parity but did so as **twelve separate
tools** — `music`, `music_play`, `music_list`, `music_next`, `music_new`,
`music_get`, `music_remove`, and five `rec_*` — with `music` overloading on/off
through a `mode` string while every other verb was its own tool. Two shapes for
one verb family, and no MCP structure matching the CLI's `vox music` group.
Authored input was split too: `music on` built a `PromptSet` (base + 12
variations); `music new` sent a bare prompt string that bypassed it; and the CLI
could not author a pool at all.

### Decision

One MCP tool per command group, named for the group, with the subcommand as its
first argument — mirroring the CLI's `vox <group> <subcommand>` exactly.

- **`mic:music`** takes `subcommand` in `{on, off, play, next, new, list, get,
  remove}`; **`mic:rec`** takes `subcommand` in `{new, list, play, get,
  remove}`. `subcommand` is a `Literal`, so the schema shows the model the exact
  verbs and an invalid one cannot be sent. The twelve old tools are deleted.
- **The mapping is uniform** across every group: `vox <group> <subcommand>`
  corresponds to `mic:<group> subcommand=<subcommand>`. The CLI and MCP are two
  views of one structure; podcast and audiobook (Phases 2–3) inherit it.
- **One `PromptSet`, built by both surfaces and sent to the daemon.** `music
  new` builds `PromptSet.single(prompt)`; the wire key is `base_prompt`; the
  bare-string path is deleted (forward integration, no shim).
- **The CLI gains `vox music on`**, reading the 12-variation pool as JSON on
  stdin (`cat pool.json | vox music on`) — no LLM in the CLI, just the structured
  input a model or a script supplies. Generation runs daemon-side.
- **`--json` works in every position** on the group subcommands (fixes vox-cnak).
- Dispatch is an explicit method table keyed by the `Literal`, never an
  `if`-ladder or `getattr` (PY-TS-11, PY-OO-6).

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|------------------|
| Keep one tool per subcommand (`music_play`, `rec_new`, …) | Twelve tools for two groups, inconsistent with `music`'s mode-arg overload; the model sees a flat list, not the group structure the CLI already has. |
| Name the first argument `mode` (as `music` did) | `mode` is music-specific; `subcommand` is uniform across every group, so rec/podcast/audiobook read the same. |
| A `--file` flag for the CLI pool input | Naming a file duplicates what the shell already does; stdin is the Unix idiom and adds no new surface. |
| Leave `music new` on a bare string | The daemon never receives the authored-input object, so "one object both surfaces build and send" fails and the spoken formats would inherit the split. |

Extends DES-050; part of the Audio Programs epic (vox-ys1p).

## DES-052: Music Player Phase 1 — voxd as a Lux Client via the Public `LuxRestClient`

**Date:** 2026-07-29
**Status:** SETTLED
**Topic:** Giving `voxd` a lux display surface for its saved-album catalog

### Context

`voxd` owns the audio device and the saved-album catalog (DES-041, DES-049). The
Music Player is a new headless app **inside `voxd`** — per WORKFLOW invariant 9,
work touching audio and daemon-owned state routes through the daemon, so the
player and its lux connection live in `voxd`, not a client. Phase 1 ships the
push-only leg: an `AlbumListScene` projection of `(catalog albums, now-playing)`
`PUT` to `/scenes/vox.music`, re-pushed whenever voxd's state changes. The player
owns no playback state — it is a pure projection of voxd's one active source, plus
a command translator back.

### Decision

`voxd` holds lux's public **`LuxRestClient`**, never the Hub-internal
**`DisplayClient`**. `DisplayClient` renders but its clicks dispatch into a void
(guard-enforced, being deprecated for apps, already flagged by the lux z-spec);
`LuxRestClient` is the public, typed, validated surface, and raw REST to `luxd` is
forbidden (Jim's ruling — validation and typing). Identity is `kind=app`,
automatic via the client.

A voxd-internal `ChangeListener` PubSub seam on `ProgramService` fires after every
applied command, every auto-advance, and every catalog edit. The listener builds
the scene and hands it to a **latest-wins mailbox** drained by an async publish
task, so the `ControlChannel` single-writer never blocks on `luxd`: a slow or
unreachable display is logged and dropped, never propagated into audio control. A
dead display can never freeze audio.

### Consequences

- New package `src/punt_vox/voxd/music_player/`; the change signal is
  voxd-internal, not gated on lux's PRs — a read-only scene that lies is worthless.
- The player re-implements no playback: play/stop/now-playing map to the existing
  `replay_album` / `stop` / `status` / `catalog_albums` primitives.
- Cross-references DES-028 (voxd is the audio host), DES-041 (Program/catalog
  model), DES-049 (daemon owns audio + files), and the `PlayerView` model
  (DES-053). Architecture settled cross-repo with the lux agent.

Design of record: `docs/vox-music-player.md`. Commits 27bbe34, 19574c0 (vox-efa6).

## DES-053: PlayerView State Model — idle/playing Projection with Invariants I1–I3

**Date:** 2026-07-29
**Status:** SETTLED
**Topic:** The formal model for the Music Player's now-playing state

### Context

The z-spec gate requires a `fuzz`-clean model for a stateful audio subsystem
before implementation. The player's playback transitions (play / stop / track-end)
are already `StartRadio` / `RadioOff` / `RadioRotate` on a single-album selection,
proven in `docs/audio-programs.tex`; re-modeling them would duplicate a proven
model.

### Decision

Model only the genuinely new content: **`PlayerView`**, a frozen value derived
from `ProgramStatus` with `mode ∈ {idle, playing}`, the playing `album` (≤ 1), and
the `NowPlaying` cursor. Three invariants live in the schema predicate:

- **I1** at most one album playing (`#album ≤ 1`).
- **I2** now-playing present iff playing (`mode = playing ⟺ #album = 1 ⟺ #nowPlaying = 1`).
- **I3** a played album is catalogued (`album ⊆ catalogued ids`).

`docs/vox-music-player.tex` states these as a small self-contained machine,
`fuzz -t`-clean, and proves each player transition preserves them. They are
consequences of voxd's single-active-source model, not new runtime checks.

### Consequences

- The connection / subscribe / lease lifecycle is a named, pending addition,
  deferred until lux pins its subscribe API.
- Extended to the `paused` mode by the Phase-3 transport model (DES-055).
- Cross-references DES-052 (Phase 1) and DES-041 (the reused Radio machine).

Ships with commit 19574c0 (vox-efa6).

## DES-054: Music Player Phase 2 — Interactive Receive Leg (Hub-Publish / voxd-Subscribe)

**Date:** 2026-07-29
**Status:** SETTLED
**Topic:** Making the `vox.music` scene interactive

### Context

Phase 1 (DES-052) can render Play/Stop buttons, but a click is inert until `voxd`
subscribes to their events.

### Decision

In-scene `ButtonElement`s carry a `publish` attribute; their Hub-side handlers
publish `music.play {album_id}` / `music.stop`. `voxd` subscribes over the
persistent `LuxRestClient` WebSocket extension (`LuxSubscription`), decodes each
message into a `PlayerEvent` — a `PlayAlbum` / `StopMusic` discriminated union,
each with a polymorphic `apply(service)` (no `if`-ladder, per oo.md) — and calls
the existing `replay_album` / `stop` primitive. The Phase-1 change signal then
re-pushes the scene, so it reflects the change. A "Music" menu callback (a ~30s
lease renewed by contact) opens the scene.

### Consequences

- All new logic stays in `voxd/music_player/` (`player_events.py`,
  `lux_subscription.py`); `client.py` is untouched — the daemon/client boundary
  holds.
- Cross-references DES-052 (the push leg) and DES-053 (the state model).

Commit 325a4f6 (vox-efa6).

## DES-055: Music Player Phase 3 — Transport State Machine (idle/playing/paused)

**Date:** 2026-07-30
**Status:** SETTLED
**Topic:** Pause/resume/prev/next transport controls on the `vox.music` scene

### Context

Phases 1–2 had two modes (`idle`, `playing`). A transport bar adds a real
pause/resume and part navigation — a stateful-audio change, so it carries its own
`fuzz`-clean, ProB-checked Z model *before* implementation
(`docs/vox-music-player-transport.tex`).

### Decision

Three modes — `idle` / `playing` / `paused`.

- **pause/resume** — the state machine is mechanism-independent: `paused` holds
  the cursor and does not auto-advance (invariant T3). The pause *mechanism* took
  three attempts. `SIGSTOP`/`SIGCONT` (freezing the process) underran the audio
  device and popped; a graceful `SIGTERM` stop with an `ffplay -ss` reseek on
  resume still popped on the kill and stuttered on resume (the wall-clock seek
  overlapped already-buffered audio). Both failed the by-ear gate and were
  rejected. The **settled mechanism is a persistent `mpv` over JSON IPC
  (DES-061)** — `set_property pause` freezes the decoder in place, click-free and
  gapless — which also supersedes the per-part `ffplay`/`afplay` music subprocess
  of DES-030.
- **prev/next** move the part cursor within the now-playing album (floored at 1,
  capped at M) without un-suspending.
- **play is start-or-SWITCH** — the resolved "Fork A": `play(album)` from `idle`
  starts it; a `play` while another album is active is a `SwitchSelection`, never
  a second source (T1).
- One play/pause **button** whose glyph and `publish` the projection sets from the
  current mode (`⏸` + `music.pause` when playing, `⏵` + `music.resume` when
  paused), so the daemon always receives one unambiguous transition.

The daemon gains `pause()` / `resume()` / `prev()` alongside `advance()` / `stop` /
`replay_album`, each with a non-UI CLI/MCP caller mirroring `next`.

### Consequences

- Seven modeled invariants T1–T7 (single active source; now-playing iff active;
  paused-is-suspended; transition guards; cursor bounds; glyph-reflects-state;
  catalogued). Extends DES-053's I1–I3 to the `paused` mode; the playback
  *mechanism* is the persistent mpv player of DES-061 (which retired DES-030's
  subprocess model).

Design: `docs/vox-music-player-transport.md`; Fork A resolved to SWITCH in commit
2c5cf57. Commits 860edb4, 925782a, 190f37c (vox-tqo1).

## DES-056: A Failed Play/Stop Is Surfaced in the `vox.music` Scene

**Date:** 2026-07-30
**Status:** SETTLED
**Topic:** Making a failed lux click observable where the user clicked

### Context

DES-040 established that a daemon failure a client cares about must be observable
through the client interface, not only a log. The lux scene is such a client
interface: a Play/Stop click that fails silently is the lux analogue of the
invisible music-generation failure DES-040 fixed.

### Decision

A failed Play/Stop surfaces in the `vox.music` scene itself, through a
`PlaybackNotice` status slot the projection renders, so the person looking at the
panel sees the failure where they clicked — never only in `vox.log`. This is
DES-040's client-observable-failure principle applied to the lux surface.

### Consequences

- The `AlbumListScene` / transport projection carries the notice; the phase-1
  change signal re-pushes it like any other state change.
- Cross-references DES-040 (the daemon-API analogue) and DES-052 (the scene).

Commit 38e0a91 (vox-xvaw).

## DES-057: Album Title Authored at Music Generation

**Date:** 2026-07-30
**Status:** SETTLED
**Topic:** Giving a saved album a human title that rides its ID3 tags

### Context

Albums were identifiable only by the style/name derived from the pool. The catalog
scene (DES-052) and the CLI listings read better with a human title, and the title
belongs on the audio itself so it survives replay and export without the manifest.

### Decision

The agent supplies a human album title at generation time — on `music on` /
`music new`. It becomes the album's name and is written into every part's ID3
tags: the Program name rides the `TALB` frame and the part/variation rides `TIT2`
(`voxd/programs/part_tags.py`), so the title travels with the file.

### Consequences

- The catalog projection (DES-052) and `music list` / `get` show the authored
  title.
- Cross-references DES-041 (Program manifest + ID3 tags) and DES-035 (track
  naming).

Commit e6950ca (vox-cdvk).

## DES-058: `[lux]`-Prefixed Lifecycle Observability Across the Music-Player Lux Legs

**Date:** 2026-07-30
**Status:** SETTLED
**Topic:** Proving the cross-process lux legs actually connect, subscribe, and push

### Context

DES-046 established that a soft, cross-boundary mechanism must emit a stable,
greppable trace so a human can prove the chain fired. The music-player lux legs
(connect, register menu, subscribe, push a scene, reconnect) span `voxd` ↔ `luxd`
and fail silently when `luxd` is down.

### Decision

A single `LuxTrace` logger emits one `[lux]`-prefixed line per lifecycle
transition — connect / register / subscribe / push / reconnect at INFO, a
recoverable down/retrying `luxd` at WARNING, a refused operation at ERROR — so
`grep '[lux]'` reconstructs the whole leg. This is in the DES-046 lineage (a
greppable proof-trace for a mechanism invisible until you can grep it),
specialized to the lux transport rather than the vibe/music chain.

### Consequences

- `voxd/music_player/lux_trace.py` centralizes the prefix; the scene publisher,
  the subscription, and the menu emit through it.
- Cross-references DES-046 (the `[vibe-trace]` lineage) and DES-048 (one
  `vox.log` — the persistent sink).

Commit 52b041c.

## DES-059: Per-Repo Enablement Marker and Plugin-less (`--no-plugin`) Install

**Date:** 2026-07-28
**Status:** SETTLED
**Topic:** Standards conformance — explicit per-repo enablement and a CLI-only install path

### Context

The org tool standard is per-repo enablement via a committed marker (the
biff/beadle/lux pattern) plus a surface that works without the Claude Code plugin.
vox instead chimed and narrated wherever it was installed, and shipped only as a
plugin. Both gaps close in one PR/rollback unit (#375).

### Decision

- **Per-repo enable/disable.** `mic:enablement action="enable"` (or `vox enable`,
  `/enable`) deposits the guide, writes the committed `.punt-labs/vox/enabled`
  marker, adds the `@`-import, and registers settings; `disable` reverses it
  (`--purge` also removes the subtree). Enable is idempotent — re-running upgrades
  the deposited guide. Both surfaces write the *same* marker so CLI and MCP agree;
  neither runs git — the marker is committed via a PR. vox chimes and narrates
  only where the marker is present.
- **`install.sh --no-plugin` / CLI-only install.** A plugin-less install path for
  non-Claude harnesses or plugin-restricted environments: `vox` on `PATH`, the
  same engine, driven through the `vox` CLI with no MCP surface.

### Consequences

- The MCP `enablement` tool and the CLI `enable` / `disable` are thin doors to one
  marker (one engine, thin clients).
- Cross-references DES-036 (the `.punt-labs/vox/` config layout) and DES-042
  (CLI/MCP surface parity).

Commit 25ec048 (#375, vox-ck3w).

## DES-060: Enablement Lives Under `/vox`, Not as Top-Level Slash Commands

**Status:** accepted. Supersedes the `commands/enable.md` + `commands/disable.md`
split-out from the enablement design (`docs/vox-enable-disable.md`).

### Context

The enablement feature (DES-051-era, vox-ck3w) shipped `enable` and `disable` as
their own top-level slash commands, `/enable` and `/disable`. Claude Code
installs a plugin's commands into a single global namespace, so those two bare
verbs claimed `/enable` and `/disable` for every session — generic names no
plugin should own. Every other vox slash verb is already namespaced under
`/vox` (`/vox model`, `/vox provider`), dispatched by parsing `$ARGUMENTS` in
`plugin/commands/vox.md`.

### Decision

Fold enablement into `/vox` as two more `$ARGUMENTS` subcommands beside `model`
and `provider`: **`/vox enable`** and **`/vox disable`**. Both call the same
`mic:enablement` tool (`action="enable"|"disable"`) with the same confirmation
text the split-out commands used. `commands/enable.md` and `commands/disable.md`
are deleted (forward integration, no shim), and `plugin/hooks/session-start.sh` lists
them among the retired commands it cleans, so an already-installed plugin drops
the stale top-level `/enable` / `/disable` on the next session start. The CLI
verbs `vox enable` / `vox disable` are unchanged — the collision was only on the
plugin's shared slash-command namespace, which the CLI does not touch.

### Alternatives Considered

| Alternative | Rejected Because |
|-------------|------------------|
| Keep `/enable` and `/disable` as top-level commands | Two generic verbs squat the global slash namespace; conflicts with any other plugin and reads as un-namespaced. |
| Alias the old commands to `/vox` (compat shim) | No installed base to migrate; a shim is complexity for zero reason (forward integration). The retired-command cleanup removes the old files instead. |

## DES-061: Persistent mpv Program Player over JSON IPC — Two-Tier Audio

**Date:** 2026-08-01
**Status:** SETTLED
**Topic:** The program audio tier (music now; audiobooks/podcasts later) runs on
one persistent `mpv` controlled over IPC; mpv is a hard dependency.

### Context

The Phase-3 transport (DES-055) needed a real, click-free pause/resume. Two
mechanisms failed the by-ear gate: `SIGSTOP`/`SIGCONT` froze the playback process
(device underrun → pops), and a graceful `SIGTERM` stop with an `ffplay -ss`
reseek on resume still popped on the kill and stuttered on resume (the wall-clock
seek overlapped already-buffered audio). Kill-and-respawn fundamentally cannot do
gapless, click-free pause.

### Decision

Two audio tiers, deliberately:

- **Notifications** (chimes, spoken quips) keep the built-in per-shot players —
  `afplay` on macOS, `say`/`espeak` on Linux — zero-install, no pause needed.
- **Programs** (music; audiobooks and podcasts to come) run on ONE persistent
  `mpv` process, opened once and driven over its `--input-ipc-server` JSON IPC
  socket: `loadfile` to play a part, `set_property pause` to pause/resume (the
  decoder freezes in place — gapless, click-free), `stop`, and the `end-file`
  event to drive auto-advance.

`mpv` is a HARD dependency — no fallback, no `if mpv … else …`. A missing or
too-old mpv (< 0.35) runs a bounded retry-to-`failed` path and surfaces
`PLAYER_UNAVAILABLE` on `ProgramStatus.playback_error`; the daemon stays up and
the independent notification tier keeps working. `install.sh` installs mpv beside
ffmpeg and `vox doctor` errors on a missing or too-old mpv or ffmpeg. The per-part
spawn-and-kill ffplay path (DES-030's subprocess mechanism, DES-055's
graceful-kill+seek) is deleted (forward integration, no shim).

### Consequences

- The mpv process/connection lifecycle (`down`/`starting`/`ready`/`crashed`/
  `restarting`/`failed`) is a stateful subsystem, so it carries its own
  `fuzz`-clean, ProB-checked model (`docs/mpv-program-player.tex`), distinct from
  the unchanged source state machine (DES-055's T1–T7): invariants I1–I7 plus
  single-`loadfile`-ownership (the loop owns `loadfile`; the supervisor only
  spawns/restarts). A crash resolves the loop's await and every pending command
  (no orphaned await); a startup that never connects and a crash loop both
  terminate at the same `failed`/`PLAYER_UNAVAILABLE` state. An unclean daemon
  exit's orphaned mpv is reaped by pid on the next start (I2 across restarts).
- Overlay (a chime over ducked music) is two concurrent OS-level streams —
  inherent to the two tiers, not debt.
- Supersedes the pause *mechanism* of DES-055 and the music-subprocess mechanism
  of DES-030; the DES-055 idle/playing/paused state machine and T1–T7 stand
  unchanged (mpv is a mechanism swap under the same model).

Design: `docs/mpv-program-player.md`; model: `docs/mpv-program-player.tex`.
Commits 558ae3f, 9549906 (hardening), 0ecfd6f (install/doctor gate).

## DES-062: Transport Verb `stop` and Last-Played `play`

**Date:** 2026-08-02
**Status:** SETTLED
**Topic:** Naming the halt verb and defining the no-argument `play`

### Context

The music transport reads play / pause / resume / prev / next, but the halt
verb was `off` (paired with `on`), out of step with that media-player
vocabulary and with the lux transport's "Stop" button. Separately, `music play`
with no argument silently started the first album in the catalog — a surprising
default with no relation to what the user last heard.

### Decision

**Rename the halt verb `off` → `stop` on every surface**, forward-integrated with
no alias (PL-PP-1): the CLI (`vox music stop`), the MCP `music` tool
(`subcommand="stop"`), and the daemon's `program_stop` wire method and
`ProgramService.stop()`. `on` is unchanged — it starts the generative radio, a
distinct action from replaying a saved album. The lux receive-leg `music.stop`
topic already matched the new name; its `StopMusic` event now calls
`service.stop()`.

**A no-argument `play` replays the last-played album.** `ProgramService` records
the id of each single album it replays (`replay_album`) in an ephemeral,
daemon-owned `LastPlayed` register. `SelectHandler` routes a request with no
album id and no tags to `ProgramService.replay_last()`, which repeats that album
or raises `no album played yet; specify an album by id, name, or style/vibe` when
none has played. The CLI and MCP surfaces detect the empty request and, on that
reject, print the saved-album list beside the message so the caller can pick one
— they never fall back to an arbitrary album. The register is ephemeral: a daemon
restart clears it, and the next bare `play` reports no history rather than
migrating any state (no persistence, no `vox.md` touch).

### Consequences

- The status-projection responsibility (`_active_status` / `_playback_fault` /
  the radio now-playing view) is extracted from `ProgramService` into
  `StatusProjection`, paying down the service god-module while the last-played
  register is added.
- No album-history persistence: last-played is in-memory only, consistent with
  the daemon-is-audio-host invariant and DES-049.

Design of record: this ADR. Closes the transport-verb polish (vox `fix/lux-observability`).

## DES-063: The Shippable Plugin Surface Lives in `plugin/`

**Date:** 2026-08-19
**Status:** SETTLED
**Topic:** Repository layout for a `git-subdir` marketplace install

### Context

The marketplace entry for `vox` used the `url` source, which clones the whole
repository into the plugin cache. Everything a user installs beyond
`.claude-plugin/`, `commands/`, and `hooks/` is dead weight for them: `src/`,
`tests/`, `docs/`, `scripts/`, `tools/`, `typings/`, `.github/`, and this repo's
own `.beads/`, `.punt-labs/`, and `.claude/` working state. None of it is
reachable from a hook or a command, because the MCP server is the `vox` binary
on `PATH` (`plugin.json` → `vox mcp`) and not code that ships with the plugin.

Claude Code offers a `git-subdir` source — a blobless partial clone plus
`git sparse-checkout set --cone <path>` — but it can only exclude whole
directories under one root. That requires the surface to sit in one directory,
which it did not.

### Decision

`.claude-plugin/`, `commands/`, and `hooks/` move under a single `plugin/`
directory, and the marketplace entry becomes
`"source": "git-subdir", "path": "plugin"`.

Measured against this branch on GitHub: a `--filter=blob:none` clone with
`sparse-checkout set --cone plugin` materializes **47 files / 2.0 MB** of
working tree (4.1 MB including `.git`), versus **1,019 files / 11 MB** (14 MB
including `.git`) for an equivalent shallow full clone — a 22x file-count and
5.5x working-tree reduction. `plugin/` itself is 124 KB.

Cone mode always materializes the files sitting in the *repo root*, so ~1.9 MB
of the remaining 2.0 MB is root documents and root state, not the plugin. In
this repo the largest contributors are the OO-ratchet artifacts
(`.oo-audit.jsonl` 460 KB, `.oo-coupling-audit.jsonl` 176 KB,
`.oo-baseline.json` 100 KB), `uv.lock` (272 KB), `prfaq.pdf` (220 KB),
`CHANGELOG.md` (188 KB), and `DESIGN.md` (168 KB). Shrinking that remainder
means moving root files into a subdirectory; this decision does not attempt it.

### Consequences

- **`${CLAUDE_PLUGIN_ROOT}` is `plugin/`.** `hooks.json`'s
  `${CLAUDE_PLUGIN_ROOT}/hooks/*.sh` entries stay correct because the whole
  hooks directory moved together, and `session-start.sh` derives `PLUGIN_ROOT`
  from its own location, so its `.claude-plugin/plugin.json` dev-mode probe and
  its `commands/` deployment follow the move without edits.
- **Nothing in the surface may reach outside itself.** A hook script may use
  `$HOME`, the `vox` binary, and paths under the *consumer's* repo root. A
  reference to any other path in this repository would resolve to a file that
  does not exist on an installed plugin. This is now an invariant of the
  layout, not an accident of it.
- **Dev loading is `claude --plugin-dir plugin`,** not `--plugin-dir .`: the
  directory has to be the one the marketplace source checks out, or every
  `${CLAUDE_PLUGIN_ROOT}/hooks/*.sh` path is wrong in dev and right in prod.
- **The repo's own references had to move with it** — the shellcheck globs in
  the `Makefile` and the lint workflow, `scripts/check-skill-permissions.sh`,
  the two release scripts, and the four test modules that locate hook scripts
  relative to the repo root. There is no packaging coupling: the wheel ships
  `src/punt_vox` via `uv_build`, and the one `importlib.resources` consumer
  reads `punt_vox.assets`.
- No user-visible behavior change. Existing installs are unaffected until the
  marketplace entry is repointed at the post-restructure release.

### Alternatives Considered

- **Keep the `url` source and accept the full clone.** Rejected: it is a 22x
  file-count penalty on every install and upgrade, for content the user cannot
  use.
- **Move the root documents into a subdirectory in the same change** to shrink
  the ~1.9 MB cone-mode remainder. Rejected as separate: it churns every
  inbound link (`README.md`, `prfaq.pdf`, `CHANGELOG.md` are referenced from
  outside this repo) and none of it blocks the `git-subdir` switch.
- **Ship the MCP server inside the plugin** instead of relying on the `vox`
  binary on `PATH`, which would make the surface self-contained but reintroduce
  the Python tree the move exists to exclude. Rejected: the two-part install
  (pip binary + marketplace plugin) is the standing design (DES-059).

Design of record: this ADR, and the rollout spec shared by the nine non-pilot
plugin repos. Pattern copied from the ethos pilot.

## DES-064: Conversation Mode Session Attachment — Headless `--resume` Subprocess, Stream-JSON Both Ways

**Status:** recommended, pending operator ratification (vox-gs9u.1, Slice
1a). Design of record: `docs/conversation-mode-session-attach-adr.md`; call
state model: `docs/conversation-mode-call-state.tex`.

### Context

`docs/conversation-mode-prd.tex` FR-4 requires a Conversation Mode call to
use the human's already-running Claude Code session, carrying its existing
task context, not a fresh context-free one. None of the five Conversation
Mode spikes tested programmatic injection of a transcribed turn into an
independent, already-running session, or programmatic extraction of that
session's streamed reply — every spike drove the authoring session itself
by hand. This was the largest named gap in the PRD's Chapter 2, explicitly
left with no recommendation pending investigation.

### Decision (recommended, not yet ratified)

Investigation of the installed `claude` CLI's documented flags found a real
mechanism: `voxd` discovers the user's active session ID via `claude agents
--json --cwd <path>`, then spawns `claude -p --resume <id> --input-format
stream-json --output-format stream-json --include-partial-messages` per
human turn — a subprocess that resumes the named session's full
conversation history, accepts one JSON user-message per turn on stdin, and
streams JSON assistant-message deltas back on stdout, including partial
chunks (feeding FR-11's "speak on the first complete portion"
requirement directly). No custom IPC is needed — the PRD's own open
question ("does it require IPC?") is answered by this subprocess's
documented stdin/stdout JSON protocol.

### Rejected Alternatives

- **Hook into Claude Code's own hook surface** (SessionStart,
  UserPromptSubmit, Stop). Rejected: hooks are reactive to events already
  happening inside a session; nothing lets an external, unrelated process
  inject a new turn into an otherwise-idle session.
- **A queue the session polls.** Rejected: nothing in Claude Code's
  documented surface lets a running interactive session poll an external
  queue mid-conversation; building it would mean modifying Claude Code
  itself, out of scope for a vox change.
- **Run the agent inside `voxd`'s own process space.** Rejected: this would
  be a second, disconnected agent instance, not the session the user was
  already in (defeats FR-4's actual point), and would reimplement tool
  execution and context management the `claude` binary already owns.

### Open Risk, Not Yet Resolved

Concurrent-resume safety — whether Claude Code's session storage tolerates
the user's interactive terminal and a headless `--resume` of the same
session ID running at the same time — is unverified from this
investigation. This is the load-bearing precondition for the recommended
mechanism and is Slice 1b's first task: a spike against a real running
interactive session, before any further plumbing is built on the
assumption.

See `docs/conversation-mode-session-attach-adr.md` for the full
investigation, the CLI evidence, and the explicit operator escalation
(three decisions: confirm the mechanism, confirm the FR-4 reading it relies
on, confirm the concurrent-resume spike as Slice 1b's first task).

---

## DES-065: DES-064's Per-Turn Subprocess Spawn — Confirmed Unworkable, Superseded

**Status:** rejected, with real measurement. Supersedes DES-064's
recommendation for the per-turn cadence specifically; does not reverse the
underlying mechanism (a headless `claude -p`-family subprocess speaking
stream-json is still the right shape — see DES-066). vox-gs9u.2 (Slice 1b)
implemented DES-064 as designed and shipped it; this entry records what
happened when it was driven by a real human, repeatedly, on 2026-08-23.

### What was implemented

DES-064's design exactly: `SessionDiscovery` finds a candidate session via
`claude agents --json --cwd <path>`; `ClaudeSessionAttach._spawn` runs
`claude -p --resume <id> --input-format stream-json --output-format
stream-json --include-partial-messages --verbose` fresh, per human turn,
writes one JSON user-message to its stdin, and reads streamed
assistant-message deltas back. Real ElevenLabs STT and TTS, real
`sounddevice`/PortAudio microphone capture, a real turn-detection state
machine, cross-process lock files for `vox call start|stop|transfer` — all
built and working. Five review rounds converged clean. This is not a
report of broken code; the code does exactly what DES-064 asked for.

### What broke, with numbers

Live operator testing plus a 20-run repeatable benchmark
(`.tmp/bench.sh`, not committed — script itself is throwaway, the numbers
below are not) found:

- **Per-turn wall clock: 13–25s median**, even against a session with zero
  accumulated context. A fresh-session run's own `result` frame reported
  `duration_api_ms: 2243` (2.24s, the real model call) against a 13.2s
  external wall clock — the remaining ~10s is process spawn plus
  bootstrap, external to the agent's own accounting entirely.
- **The dominant cost is Claude Code's `SessionStart` hook cascade**,
  re-paid on *every single turn* because a fresh subprocess is spawned per
  turn: 9 hooks on a bare fresh session, 28+ on a session with real
  history (`--resume` re-fires `SessionStart:resume` for every registered
  plugin — this repo's own dev environment alone registers ~15).
  `--bare` (Claude Code's own "skip hooks, LSP, plugin sync,
  auto-memory..." minimal mode) eliminates the cascade entirely — verified
  empirically, zero hook frames — but requires `ANTHROPIC_API_KEY`
  explicitly; it does not support OAuth at all
  (`claude --help`: "Anthropic auth is strictly ANTHROPIC_API_KEY or
  apiKeyHelper via --settings"). Wiring it in is therefore not a free
  win: it inverts the auth model DES-?? (the `078a841` fix, same session)
  had just corrected the other direction, and ships as a real breaking
  change — `vox call` stops working on an OAuth-only setup.
- **`--resume` is itself a recurring source of fragility**, independent of
  the hook cost: a stale `ANTHROPIC_API_KEY` in the parent shell silently
  shadows the target session's real login and fails auth (fixed,
  `078a841`); resuming a session that is concurrently busy (the
  interactive terminal actively mid-turn — DES-064's own named "open
  risk, not yet resolved") hangs until the reply timeout; a `--bare`
  invocation against a genuinely idle session, with auth and hooks both
  confirmed correct, still hit the full 120s timeout live with the
  spawned process visibly alive and in I/O wait (`ps` showed 1.11s of CPU
  time consumed across the whole wait) rather than crashed — consistent
  with either real API-side slowness or a `--resume`-specific cold-load
  cost on the target session's history that `--bare` does not address,
  neither root-caused.
- Net operator assessment, verbatim: **"It's never going to work."**
  Spawning a fresh general-purpose CLI, with its full plugin/hook
  ecosystem, once per conversational turn, is confirmed the wrong shape
  for a live voice exchange — not a tuning problem.

### What is retained

`SessionAttach` (DES-064's own abstraction) is a `Protocol`; `CallSession`
and everything above it depend on the interface, not on
`ClaudeSessionAttach`'s implementation. Real mic capture, real STT, the
turn-detection state machine, the call lock/control mechanism, the call's
own lifecycle state machine, the Slice 2a playback-sink ordering work, and
the turn-timer diagnostics infrastructure are all implementation-agnostic
to what sits behind `SessionAttach` and are unaffected by this rejection —
roughly 88% of Slice 1b's ~9,200-line diff by line count. The ~12% being
discarded is exactly `claude_session_attach.py`, `claude_subprocess_env.py`,
`session_discovery.py`, `session_attach.py`, and their tests — the part
DES-064 itself flagged as swappable by naming it behind a `Protocol`.

### What is not retained

Do not build further on the per-turn-spawn cadence. No more timeout
tuning, no more `--bare`/OAuth auth-model chasing, no more `--resume`
concurrent-access workarounds. The mechanism needs to change shape, not
receive another patch — see DES-066.

---

## DES-066: Persistent Call Agent via `pi --mode rpc` — Mechanism Spiked, Confirmed, and Ratified

**Status:** RATIFIED 2026-08-24 (`vox-m2ss`, operator: "yes, I sign off on
context snapshot not literal session resume"). Core mechanism confirmed
live (2026-08-23 spike, below); read-only enforcement confirmed live
(`--tools read,grep,find,ls`, below); context-handoff and summary-handoff
design completed in DES-067. The one remaining open item is the
process-supervision layer (`tmux`/`keep` vs. a direct `subprocess.Popen`)
— recommended, not blocking, per the PRD's own framing. `vox-hobl.2` is
the implementation mission this design now dispatches against.

### Context

DES-065's numbers all trace to one shape: a subprocess spawned fresh, per
turn, that must cold-boot Claude Code's full plugin/hook ecosystem and
(via `--resume`) load a specific existing session's history, every single
exchange. The fix is not a faster spawn — it is not spawning per turn at
all.

### Proposed decision

Spawn one agent process per *call*, not per turn, and keep it alive for
the call's full duration via `tmux`, using the `keep` extension already
built and shipped in `../pi-tools` (`keep_run` starts a long-running
interactive process in a tmux session; `keep_send` writes one line to it;
`keep_capture`/`keep_watch` read its current output, the latter with a
wake policy; `keep_stop` tears it down) — the same mechanism `pi-tools`
already uses to chain one `pi` instance to another. Concretely, per call:

1. `keep_run` starts one `pi --mode rpc` (or `claude`, TBD by the spike
   below) process in a dedicated tmux session at call start.
2. Each human turn is `keep_send` (or the RPC-mode stdin protocol
   directly, if driven from Python rather than through `pi-tools`' own
   tool surface) — no new process, no re-paid bootstrap.
3. The call agent does **not** resume the primary interactive session.
   It starts fresh and receives a compact context snapshot (current
   task, relevant files, recent conversation gist) as its opening
   message — satisfying FR-4's actual intent ("carrying task context")
   without inheriting `--resume`'s cold-load cost, concurrent-access
   hazard, or auth-model coupling to the primary session's own login.
4. The call agent operates **read-only against the codebase/project**
   for the call's duration. It may update beads or other out-of-band
   coordination state live (the restriction is repo/codebase file
   writes specifically, not every side effect).
5. At call end, the call agent's conversation is summarized and handed to
   the primary session; the primary session is the only one that ever
   applies repo/codebase writes, and only after the call, from that
   summary.

### Why `pi` over `claude` for the agent inside the tmux session

Measured 2026-08-23: `pi --list-models` (loads config, model catalog,
provider connections — no API call) completes in ~1.35s, versus Claude
Code's ~10–20s of hook/plugin bootstrap for the equivalent no-op. `pi`'s
own documented design principle is a minimal core that deliberately
excludes MCP, sub-agents, permission popups, and background bash, pushing
them to opt-in extensions — the inverse of Claude Code's always-on plugin
ecosystem, which is precisely what DES-065 found costly. `pi --mode rpc
--no-session` exposes exactly the `prompt` → `message_update` →
`agent_end` event protocol this integration needs, natively, rather than
repurposing a general interactive CLI's `-p` flag for it.

### Spike result — 2026-08-23, mechanism confirmed

Ran the exact two-turn liveness spike this entry's own "Next step" called
for: a bare Python harness (`.tmp/pi_rpc_spike.py`, scratch, not committed)
spawns `pi --mode rpc --no-session` once, writes one JSON `prompt` command
per turn to its stdin, reads JSON-line events back from stdout.

- **First attempt reproduced the original failure exactly**: prompt
  accepted (`{"type":"response","command":"prompt","success":true}`),
  `agent_start`/`turn_start`/the user `message_start`/`message_end` all
  fired, the assistant's own `message_start` arrived with
  `"stopReason":"pending"` — then nothing. No `message_update`, no
  `message_end`, no `agent_end`, for 20s+. Interleaved in the stream were
  repeated `extension_ui_request` `setStatus` events from `biff-bridge`
  (`"biff: connected"`), pi-tools' Biff-polling extension, which loads by
  default alongside `keep`.
- **Root cause, isolated by bisection**: adding `--no-extensions` (no
  other change) made the assistant stream complete normally — thinking
  and text deltas arrived within roughly a second of wall clock, both
  turns replied correctly (`PONG-ONE`, `PONG-TWO`), and the process exited
  cleanly (code 0) after stdin closed. The hang is `biff-bridge`
  interfering with the RPC stdout stream when both are active
  simultaneously in this environment, not a defect in `pi`'s core RPC
  mode. Not root-caused further than that (i.e., *why* the extension
  interferes) — unnecessary to, since the call agent has no legitimate
  reason to load `biff-bridge` in the first place: a live phone call has
  nothing to do with team messaging.
- **The persistence claim is now demonstrated, not assumed**: turn two's
  usage block shows `"cacheRead":12733` tokens against turn one's prompt
  cache, versus turn one's own `"cacheRead":0` — direct evidence the
  second turn reused the still-warm first turn's context inside the same
  live process, rather than re-establishing it from nothing. This is
  exactly the cost DES-065 identified as unavoidable in the per-turn-spawn
  design (every turn re-pays full bootstrap) and confirms the persistent-
  process shape eliminates it.

Practical takeaway for the real implementation: launch the call agent with
`--no-extensions` (or a narrower per-extension disable if `pi-tools` grows
one, once `keep`'s own bridge role is no longer needed inside the call
agent's own process — the call agent doesn't need `keep` on itself, only
the *outer* driver needs `keep` to hold the call agent's tmux pane) and
drive it with the documented RPC command/event JSONL protocol
(`prompt`/`steer`/`follow_up`/`abort` in; `message_update`/`agent_end` out)
directly, rather than through `pi-tools`' own tool surface, since the
driver here is vox's own Python daemon, not another `pi` agent.

### Remaining known unknowns

- **Read-only enforcement mechanism is undecided.** A permission-mode
  flag, a sandboxed/read-only filesystem view, and tool-level restriction
  at the harness are all candidates; none is chosen. A prompt instruction
  alone is not a mechanism.
- **The summarize-and-handoff mechanism is undecided.** Whether the
  primary session picks up the summary via a file its next
  `UserPromptSubmit` hook reads, an injected context block, or something
  else, is not designed.
- **Whether this design still satisfies FR-4** as originally read
  (resuming the *literal* session) needs an explicit operator re-read,
  since the call agent is now deliberately disconnected from the primary
  session's live state by design, not by accident.
- **tmux/`keep_run` supervision itself is not yet spiked** — this spike
  drove the RPC protocol directly over `subprocess.Popen` pipes to isolate
  the protocol question from the process-supervision question. Holding
  the same process inside a `keep_run` tmux pane and reaching it via
  `keep_send`/`keep_capture` from vox's own code (rather than another
  `pi` agent's own tool calls) is a distinct integration surface — likely
  thinner than the protocol work just proven, since `keep`'s job is just
  keeping the pane alive and returning its output, but not yet run.

### Next step

Design and Z-model the call-agent lifecycle now that the core mechanism is
proven live: process spawn/teardown per call, the context-snapshot
handoff at start, the read-only enforcement mechanism, and the post-call
summary handoff to the primary session — per this project's own rule that
a 3+-state stateful subsystem gets a Z spec before implementation
dispatches. Decide there whether `keep_run`/`tmux` or a direct
`subprocess.Popen` (as this spike used) is the right supervision layer for
vox's own daemon to hold the call agent's process across a call — `keep`
was written for an interactive `pi` session managing panes a human or
another agent inspects, not necessarily for a headless daemon; a direct
`subprocess.Popen` held by `voxd` itself, with `--no-extensions`, may be
the simpler and more directly analogous fit to the mic/STT/turn-detection
layers `CallSession` already owns, without a `tmux`/`pi-tools` runtime
dependency added to vox's own `pyproject.toml`. Decide explicitly rather
than defaulting to the `pi-tools` mechanism because it existed first.

### Two more open unknowns closed — 2026-08-23, same-session follow-up spikes

The operator's own framing narrowed the read-only requirement before this
was spiked further: the call agent does not need to avoid colliding with
the primary interactive session's own state (they are already
disconnected by this design, per the "Proposed decision" above) — the
only real risk is the call agent writing to *repo files* while a human
might be editing the same tree through the primary session. That is a
much smaller problem than a full sandbox.

- **Read-only enforcement, resolved**: `pi` has a native CLI tool
  allowlist, confirmed via context7 (`/websites/pi_dev`), not guessed --
  `--tools read,grep,find,ls` (the CLI form of the SDK's
  `tools: [...]` config). Verified live: prompted the agent to write a
  probe file with only `--tools read,grep,find,ls` set; it never invoked
  a write tool at all (no `tool_execution_start` for one), and the file
  never appeared on disk. No sandboxed filesystem, no permission-mode
  plumbing — one flag, verified working.
- **Real tool use at real latency, confirmed**: re-ran the two-turn
  liveness spike with actual file-read questions against this repo
  (`--no-extensions`, no other change) instead of canned "reply with
  exactly X" prompts. Turn one (cold, includes a real file-read tool
  call) reached first text at t+6.8s and completed at t+7.0s; turn two
  (warm) completed 4.3s later. This is the persistence win holding under
  real work, not just an echo test -- still an order of magnitude under
  DES-065's 13-25s per-turn-spawn median, which did zero tool use for
  that number.

Both were run before any further design/implementation investment, per
the standing instruction to spike first rather than build a day's worth
of code on an unverified mechanism (the same failure mode DES-065 itself
was the postmortem for).

### Security review, 2026-08-23: two gaps the `--tools` allowlist alone does not close

A read-only *tool* allowlist blocks writes; it does not address
disclosure, and this design's own threat model (per the operator: not
adversarial isolation, but avoiding accidental collision plus not handing
out credentials the call agent has no legitimate use for) has one
disclosure vector and one credential-hygiene gap. The first is now
spike-confirmed, not just reasoned:

- **Reading is a real exfiltration path here, specifically because the
  agent's output is spoken audio, not text on a screen — verified live,
  2026-08-24.** Dropped a fake secret (`FAKE_API_KEY=sk-spike-...`) into
  `.tmp/fake_secret.env` and asked the same `--tools read,grep,find,ls`
  agent to find and read it. It did, and its reply text was the exact key
  value verbatim (`The exact contents of .tmp/fake_secret.env are: ...
  FAKE_API_KEY=sk-spike-not-a-real-secret-12345`) — in production this
  text is what gets synthesized to speech and played in the room, no
  restraint applied on its own. `read`/`grep`/`find`/`ls` alone are
  sufficient to read `.env`, a credentials file, or any other secret
  sitting in the repo and narrate its contents aloud — worse than the
  equivalent in a normal Claude Code session, because a spoken secret is
  audible to anyone in the room, not just visible on a screen the human
  controls. **Action for the implementation mission**: either an
  explicit deny-list of sensitive paths (`.env*`, anything matching this
  repo's own `.gitignore` secret patterns) passed to the call agent's
  system prompt as a hard instruction, or accept the residual risk
  explicitly and document why (e.g., if the call is scoped to a
  repo/session the user already trusts with spoken output). Silence on
  this in DES-066 was itself the gap — not a decision either way.
- **`subprocess.Popen` inherits the parent environment by default, and
  `voxd` already holds provider API keys in its own env.** This project
  hit exactly this class of bug once already this session:
  `ANTHROPIC_API_KEY` shadowing OAuth login in the now-superseded
  per-turn spawn (DES-065), fixed by `ClaudeSubprocessEnv` stripping it
  before every `claude` subprocess launch
  (`src/punt_vox/voxd/conversation_mode/claude_subprocess_env.py`). The
  call-agent spawn needs the same discipline applied to whatever
  provider credentials `voxd` itself holds (ElevenLabs, OpenAI, AWS) —
  the call agent has no legitimate use for them and should not receive
  them by default just because `subprocess.Popen` passes the full parent
  environment unless told otherwise. Not yet designed; add to the
  implementation mission's contract alongside the `--tools` allowlist,
  not as a follow-on fix after the fact.

### Rejected: MCP tool access inside the call agent — 2026-08-24

Explored giving the call agent live tool access to quarry (semantic
search over source + conversation history) and context7 (docs) via pi's
third-party `pi-mcp-adapter` extension, so it could look things up
mid-call rather than being limited to `read`/`grep`/`find`/`ls` on the
repo alone. Spiked: `pi install npm:pi-mcp-adapter`, loaded explicitly
via `-e <path>` alongside `--no-extensions` (to avoid the `biff-bridge`
extension DES-066 already found interferes with the RPC stream). The
extension loaded without reintroducing the `biff-bridge` hang, but the
`--tools` allowlist turned out to filter extension-registered tools too,
not just built-ins — with `--tools read,grep,find,ls` set, the
mcp-adapter's own tool was invisible to the model regardless of whether
the extension itself was active.

**Rejected outright by the operator before further debugging**: "I do
not wish or need to use mcp with pi... do not bloat pi." The extension
was uninstalled (`pi remove npm:pi-mcp-adapter`) and no further work
went into fixing the allowlist interaction. Do not re-attempt an MCP
adapter inside the spawned `pi` process for this feature — it directly
works against the reason `pi` was chosen over `claude` in the first
place (a minimal, extension-free process is what makes the persistence
and latency wins in this document real).

**What replaces it**: quarry and context7 access happen in the *primary*
Claude Code session, before the call agent is even spawned, as part of
constructing the context snapshot — `quarry --json find "<query>"` is a
real, working CLI flag (verified live), so the primary session runs it
against a query derived from the call's topic and folds relevant hits
directly into the snapshot text handed to the call agent as its opening
message. The call agent's own tool surface is unaffected: still exactly
`--tools read,grep,find,ls`, `--no-extensions`. See `vox-hobl.1` for the
full context-snapshot design.

---

## DES-067: Context-Snapshot Construction and Post-Call Summary Handoff

**Status:** decided, per operator direction (2026-08-24). Resolves
`vox-hobl.1`, the last design work blocking the replacement `SessionAttach`
implementation (`vox-hobl.2`) besides FR-4's own ratification (`vox-m2ss`).

### Context

DES-066 named two pieces explicitly undesigned: what goes into the call
agent's opening message, and how its conversation reaches the primary
session after the call ends. Both are resolved here.

### Decision 1: the primary session constructs the context snapshot

Entry point is `/vox:call start <topic>`. The **primary session** — the
one the human is already working in, and the one that runs this command
— authors the call agent's opening context snapshot itself, from
`<topic>` plus whatever it already knows about the current task and
recent conversation. It is not an automated extraction pass run by some
third component, and it is not the call agent introspecting anything
about the primary session on its own (it cannot — DES-066 already
established the two processes are deliberately disconnected).

This resolves the "sourced from where" question DES-066 left open by the
simplest available answer: the primary session already holds the
context; handing it off is the same act as any handoff between two
collaborators, not a retrieval problem.

**Quarry and context7, folded in upstream, not granted as live tools.**
DES-066's "Rejected: MCP tool access" section (above) has the full spike
trail — summarized: the primary session runs `quarry --json find
"<query>"` (a real, verified-working CLI flag) against a query derived
from `<topic>`, and folds relevant hits directly into the snapshot text
before the call agent is spawned. The same pattern applies to context7
if a topic clearly needs external docs. The call agent's own tool
surface is unaffected — still exactly `--tools read,grep,find,ls`,
`--no-extensions` per DES-066 — because all of the extra context
gathering happens in the primary session, in Python, before `Spawn`
(the call-agent process lifecycle's own first operation,
`docs/conversation-mode-call-agent.tex` §Operations) ever runs.

### Decision 2: the summary is a structured document, not free prose

Modeled directly on `../prfaq/`'s meeting-summary format (see
`../prfaq/meetings/meeting-summary-*.md` for the reference shape used by
`/prfaq:meeting`) — a structured document with fixed sections, not a
paragraph of prose a human has to parse:

- **Decisions Made** — anything the call actually settled.
- **Action Items** — what the primary session should do next. This
  section *is* the handoff DES-066 left undesigned: it is the mechanism
  by which the call agent's read-only findings become the primary
  session's write actions, satisfying DES-066's own requirement that
  only the primary session ever applies repo/codebase writes, and only
  after the call.
- **Context Gathered** — files the call agent read, quarry hits and
  docs consulted (both the primary session's pre-call lookups and
  anything the call agent itself read live via its own `read`/`grep`/
  `find`/`ls` tools).
- **Beads Touched** — any bead the call agent created or updated live
  during the call, per DES-066's standing allowance for out-of-band
  coordination state (the restriction is repo/codebase file writes
  specifically, not every side effect).
- **Notes / Open Questions** — anything unresolved, for the primary
  session or the human to pick up.

### Decision 3: delivery is a direct file read, not a hook

The call agent authors the summary itself, as its own final turn before
`Teardown`/`AbortTurn` (`docs/conversation-mode-call-agent.tex`), written
to `user_state_dir() / "calls" / "<call-id>-summary.md"` — a new
`calls_dir()` helper alongside the existing `recordings_dir()` in
`src/punt_vox/paths.py`, following that module's established one-helper-
per-purpose convention rather than overloading `run_dir()` (which is for
ephemeral lock/pid state, not a durable record a human or the primary
session may want to revisit later, the same distinction that already
separates `recordings_dir()` from `run_dir()`).

Because the call was started **from** the primary session and that
session is still present when the call ends (`/vox:call start` is not a
detached background invocation), the simplest delivery mechanism is that
the primary session reads the summary file directly once the call
signals it has ended — no `UserPromptSubmit` hook injection, no polling.
The candidate list DES-066 originally left open (a hook the primary
session's next prompt triggers, an injected context block) assumed a
scenario where the primary session might not still be attached; that
scenario does not arise given `/vox:call start`'s own invocation shape,
so the simpler mechanism is correct, not merely convenient.

The primary session may append its own notes on read (e.g., recording
which Action Items it actually acted on) — the file is not sealed or
append-only; it is a normal repo-external artifact under
`user_state_dir()`, not a git-tracked one.

### Rejected Alternatives

- **The primary session synthesizes the summary from the raw call
  transcript**, rather than the call agent authoring it directly.
  Rejected: the call agent is closest to what happened and already has
  to produce a final reply regardless; a second synthesis pass by a
  different process would be strictly more machinery for the same
  result, and risks losing detail across the handoff it exists to avoid.
- **MCP tool access inside the call agent** for quarry/context7 — see
  the dedicated section above (DES-066). Rejected by the operator
  directly: "I do not wish or need to use mcp with pi... do not bloat
  pi."
- **A `UserPromptSubmit` hook picks up the summary** — rejected per
  Decision 3 above: unnecessary machinery for a synchronous
  primary-session-present scenario.

### Still Open

The `calls_dir()` retention policy (does every call leave a summary
file forever, or is there a cleanup/rotation policy analogous to
`recordings_dir()`'s) is not decided here — a small implementation
detail for `vox-hobl.2`'s mission, not an architectural fork.

---

## DES-072: Widget Refresh Patches an Installed Scene; `show` Is Reserved for Install and for "Bring This Window to Me"

**Status:** decided (2026-08-28). Implements `vox-h777`.

### Context

Both of vox's lux surfaces — `vox.music` in `voxd`, `vox.panel` in the
`vox-panel` applet — pushed every scene through `client.scene.show`. On
the Hub side `show` installs the tree *and* raises and unminimizes the
frame whenever the scene looks new to it. Every track change, every
generated part, every catalog add, every radio click therefore dragged
the window to the front of the user's stack, seconds apart, unprompted.

The obvious fix — replace `show` with `update` everywhere — breaks the
feature the menu entries exist for. Clicking **Music**, or **Vox**, is a
request to *see* that window; raising it is the correct answer, not a
side effect.

### Decision

One call was serving two intents, and they are now two verbs.

**Refresh** is the default. The surface holds a `LiveScene` carrying the
last render it put out, and each new render is diffed against it: an
identical tree pushes nothing at all, moved values become a
`ScenePatchSet` sent through `scene.update`, and only a changed element
roster or frame shell falls back to `show`, because no patch can express
those. `update` reaches the Hub's scene writer and touches frame, focus,
and tab state not at all.

**Install** is reserved for four call sites, two per scene, and each is
either "nothing is on this connection yet" or an explicit user gesture:

| Scene | Site | Why it installs |
|---|---|---|
| `vox.music` | hub handshake (`LuxSubscription.on_connect`) | the connection carries nothing to patch |
| `vox.music` | **Music** menu click (`on_callback`) | the user asked for the window |
| `vox.panel` | **Vox** menu click (`VoxPanelService.acknowledge`) | the user asked for the window |
| `vox.panel` | *(the first push on a connection, via the same `LiveScene`)* | nothing installed yet |

Two constraints of the patch seam shaped the rest of the change, and both
are properties of the *scene*, not of the diff:

- **A patch cannot add an element.** So `NowPlayingBlock` stopped
  emitting one element when idle and two when active; it emits two
  always, and idle changes their content rather than their number.
  Without that, pressing play — the single most consequential refresh
  there is — would have fallen back to `show` and raised the window.
- **A field that vanishes between renders is invisible to a differ.** So
  `AlbumTable._selection` always emits `selected_row_ids`, empty list
  included; omitting it when idle would have stranded the now-playing
  highlight on the row that stopped playing.

A refused patch, a refused install, and an absent luxd all *disarm* the
`LiveScene`, so the next push installs rather than patching a tree luxd
never accepted. That also covers a luxd restart with no special handling:
the first patch after it meets an unknown scene, is rejected whole
(nothing mutated), and the fallback installs. One wasted round-trip, no
stale state.

### Rejected Alternatives

- **Replace every `show` with `update`.** Rejected: it breaks the menu
  entries, which exist precisely to bring a buried window forward. The
  defect was never that `show` raises the frame — that is the feature —
  but that one call was carrying both meanings.
- **An allowlist of patchable field names per element kind.** Tempting,
  because luxd rejects a field with no `_set_<field>` method. Rejected:
  a hand-copied list of another package's setters is a second source of
  truth that drifts silently. A rejected batch mutates nothing, so the
  install fallback already handles an unpatchable field. Self-correcting
  beats synchronized.
- **Reading back the Hub's own tree to diff against.** Rejected: a
  read-back races the replicator. The surfaces diff their own two wire
  trees — the previous `RenderRequest` and the new one — which is
  authoritative for what *they* sent and needs no round-trip.

### Addendum (2026-08-29): `show` does not reliably raise the frame

The Context and Decision above assumed `client.scene.show` always raises
and unminimizes the frame it targets. It does not. The Hub's
`upsert_scene_in_frame` (`punt_lux.display.replica.scene_replica`) only
clears `frame.minimized` and grabs focus when the scene is *new* to the
frame (`is_new`, keyed on `msg.id not in frame.scenes`); a scene already
registered in the frame gets its content replaced in place and none of
that attention. Because `vox.music` and `vox.panel` both stay
permanently installed on their `LiveScene` once the first connection
lands, the scene is never new by the time a real menu click fires — so
`show`'s raise silently stopped firing on every "bring this window to
me" gesture after the very first one. Live-reproduced: minimize the
Music widget, click Clients → voxd → Music, confirm via `vox.log`
("Music menu clicked; installing the scene" / "installed vox.music
scene") that `install()` ran, and watch the window stay minimized.

The four install call sites in the table above did not change. What
changed is that each of them now *also* calls the Hub's `raise_frame`
operation (`client.frame.raise_`) explicitly, immediately after its
`show`/reinstall push lands, instead of trusting `show` to raise on its
own: `LuxScenePublisher` raises for every `SceneDelivery` whose `install`
flag is set (both `vox.music` sites), and `PanelPush.install` raises for
`vox.panel`'s one site. `show` still carries the content; raising the
frame is now a second, explicit step the same caller takes right after.
The decision to keep two verbs (refresh vs. install) stands unchanged —
only the assumption that install's raise came for free from `show` was
wrong.

---

## DES-068: E+ Umbrella — Voice Agent Hosted in `voxd` via ElevenLabs Conversational AI

**Status:** PROPOSED (2026-08-29). Reconsiders the voice-agent shape settled in DES-066 given operator assessment that no design in this thread has yet produced ratifiable UX. DES-066's ratification recorded a mechanism spike; DES-067 filled its remaining design gaps; neither has been driven repeatedly by a real user to the standard DES-065's 20-run benchmark set for its predecessor. E+ takes another swing at the same problem with a materially different LLM host and tool model. Validation beads: `vox-bst7`, `vox-73y7`, `vox-juhw`, `vox-6v7f`. Diagram artifacts under [`docs/artifacts/`](docs/artifacts/): [`e-plus-voice-architecture.html`](docs/artifacts/e-plus-voice-architecture.html) (final E+ shape, Mode A + Mode B, same-host-launch v1 default), [`e-plus-adr-revision.html`](docs/artifacts/e-plus-adr-revision.html) (two-walls framing and D vs E+ bridge document), and [`distributed-target-topology.html`](docs/artifacts/distributed-target-topology.html) (companion to DES-069 and DES-071).

### Context

Every voice-over-active-Claude-Code-session design in this repo runs into two structural constraints of Claude Code itself, worth naming explicitly since they collapse the option space to two shapes:

- **Wall 1 — no foreground command spans turns.** A slash command or tool call is bound by the session's turn model; the moment the LLM finishes its reply, the command has exited. Rules out multi-turn *and* barge-in as a foreground shape.
- **Wall 2 — no background process can inject.** Claude Code exposes no channel by which an external process hands a new user turn in. Hooks are reactive; the interactive loop is TTY-driven. Rules out any "run mic loop out-of-band, push transcript in" shape.

Every survivable design either (a) collapses the multi-turn voice conversation into a single Claude Code turn, so Wall 1 doesn't apply because there's only one turn; or (b) runs the voice conversation in a *peer process* against the primary session, so Wall 2 doesn't apply because there's no injection. DES-064 was (b) with per-turn spawn. DES-066 is (b) with a persistent peer. E+ is (a).

### Proposed decision

`/vox:talk` is one blocking Claude Code tool call. Multi-turn voice conversation happens inside `voxd`, hosted by **ElevenLabs Conversational AI** (DES-069). `voxd` maintains per-session context via a **snapshot seed** the primary session hands over at call time — retaining DES-067 Decision 1's "primary session authors the seed" insight verbatim — plus a **rolling context store** fed by the hook fanout that already delivers TTS chimes (DES-070). The voice agent's tool surface — `search_code`, `read_recent_conversation`, `read_older_conversation`, `write_note`, `launch_session` — is exposed as ElevenLabs **client tools** implemented by `voxd`. At call end, the transcript plus any `write_note` outputs are returned as the `/vox:talk` tool call's value, so the primary session resumes with them in view — a plain tool return in place of DES-067 Decision 3's file-read handshake.

A second entry point, **Mode B voice-first** (DES-071), lets the user speak to `voxd` before any coding session exists; `voxd` then launches a fresh `claude`, `pi`, or `opencode` session same-host by default, remote via SSH optionally.

E+ preserves DES-066's essential move — the call agent operates read-mostly against the codebase; only the primary session (or a Mode-B-spawned session) applies writes afterward — but shifts the LLM host, the turn-taking authority, and the tool model. The four decisions that constitute E+ are recorded separately in DES-069, DES-070, DES-071. This entry is the umbrella that names the reconsideration and the two-walls framing.

### Rejected alternatives (in this reconsideration)

- **Re-ratify DES-066 as ship-ready and move on.** Rejected: operator assessment is that the UX bar has not yet been met by any design in this thread, and DES-066's own process-supervision layer (`tmux`/`keep` vs. direct `subprocess.Popen`) is still an open item within it. Ratification of a mechanism spike is not the same as a shipping product.
- **Add EL Conv AI to DES-066 as an STT/TTS front-end only, leaving `pi --mode rpc` as the LLM.** Rejected as a stopping point: DES-065's cost pattern was cold-boot-per-turn; DES-066 solved that by making the agent persistent, at the cost of using `pi`'s general-purpose tool surface as the vehicle for what is fundamentally a voice conversation. Splitting STT/TTS from the LLM leaves the voice conversation talking to a text CLI's `-p`-family protocol rather than a voice-native turn-taking one — the shape that made barge-in hard in the first place.
- **Wait for `pi` to add streaming voice-native turn control natively.** Rejected as a bet on a roadmap this repo does not own; EL Conv AI ships it today.

### Open items / risks

- Vendor coupling to ElevenLabs Conversational AI is deeper than "just TTS" — the turn loop, tool orchestration, and barge-in behavior become EL's. `vox-bst7` is the foundation spike that either establishes or refutes the bet.
- The rolling context store's payload sufficiency has not been measured; `vox-73y7` addresses that.
- Mode B's session-launch semantics introduce a real capability escalation dressed as a tool call; DES-071 records the shape and the permission-profile mitigation.
- Nothing in E+ invalidates the ~88% of DES-064's implementation that DES-065 identified as retained (mic capture, STT, TTS, playback ordering, turn-timer diagnostics, call lifecycle machinery). E+ is a swap at the `SessionAttach` boundary the earlier design already isolated behind a `Protocol`.

---

## DES-069: Voice-Agent LLM Turn Loop — ElevenLabs Conversational AI, Client Tools Model

**Status:** PROPOSED (2026-08-29). Sub-decision under DES-068. Validation: `vox-bst7` (foundation spike), `vox-6v7f` (optional A/B against DES-066's `pi --mode rpc` shape).

### Context

DES-066 chose `pi --mode rpc --no-session --no-extensions` for the LLM turn loop because `pi`'s ~1.35s bootstrap dominated Claude Code's ~10–20s hook cascade, and `pi`'s minimal-core design principle gave a clean surface for read-only tool enforcement via `--tools read,grep,find,ls`. That reasoning was correct for a persistent-shell shape driving a text CLI's `-p` protocol. It does not extend to a voice-native turn loop, which needs streaming STT+LLM+TTS interleaved with barge-in and turn-taking as first-class concerns, not features grafted on.

### Proposed decision

Use **ElevenLabs Conversational AI** as the LLM host inside `voxd` for the duration of a call. Register the voice agent's tool surface (DES-068) via EL's **client tools** primitive:

- Tool schemas declared in the WebSocket handshake at session start.
- `client_tool_call` events processed by `voxd`; results returned via `client_tool_result` events on the same socket.
- No separate HTTP callback surface on `voxd`; no MCP inside the call agent (preserving DES-067's "do not bloat pi" operator ruling by analogy — the voice agent's tools are `voxd`-implemented and closed-world, not MCP-mediated).

Rationale:

- EL Conv AI's turn-taking, VAD, and barge-in are first-class, streaming, and voice-native.
- Client tools give `voxd` full authority over what the voice agent can do (each tool implemented locally in Python) without depending on `pi`'s general tool surface or Claude Code's hook/plugin ecosystem.
- Tool round-trips ride the same WebSocket as audio — no extra hop, no extra auth.

### Rejected alternatives

- **`pi --mode rpc` per DES-066.** Rejected here because the LLM host chosen must own voice-native turn-taking, not just streaming text. See Context.
- **Self-hosted turn loop atop the Anthropic Messages API with a homegrown VAD/barge-in state machine.** Rejected on scope: reimplementing what EL Conv AI already ships is a quarter of work per DES-065's precedent for underestimating cold-boot and integration costs.
- **OpenAI Realtime API or Google Gemini Live.** Not explored in this ADR; may be revisited if `vox-bst7` finds EL Conv AI unfit. The client-tools shape generalizes to either.

### Open items / risks

- Client-tool round-trip latency under real WAN conditions is unmeasured. Kill criterion: p95 tool round-trip ≥ 1.5s under production-like conditions (`vox-bst7`).
- Barge-in behavior mid-tool-call is unspecified in EL Conv AI's public docs; also in `vox-bst7`'s scope. If barge-in during a tool call produces confused conversation state, E+ needs redesign.
- Vendor coupling: switching LLM host in future means re-doing this ADR and the client-tools implementations, though the tool surface itself (DES-068) is transport-agnostic and would carry over.

### Validation outcome (2026-08-29, `vox-bst7`)

Both kill criteria were adjudicated under production-like conditions; the
full evidence is `spikes/vox-bst7-el-convai/REPORT.md` and its committed
traces/metrics.

- **Latency: PASS.** p95 EL-attributable tool round-trip overhead 993ms
  (< 1.5s), n=27 invocations over real WAN; robust even when every
  fast-tool sample the measurement bias could pollute is discarded.
- **Barge-in: PASS with caveat (operator-ruled).** Interrupting the agent
  mid-tool-call never corrupted conversation state — session, memory, and
  subsequent tools stay intact — but the interrupted call's result is
  deterministically dropped from the LLM context (recoverable by re-ask).
  E+ mitigation: idempotent client tools plus a `voxd`-side result cache
  so a post-interruption re-invocation is instant. For the evidence
  trail: the automated adjudicator's committed verdict artifacts
  (`results/verdict_barge_in_*.json`) record FAIL on the recall
  criterion — the dropped result is exactly what they detect — and the
  operator ruled that recoverable loss acceptable on 2026-08-29; the
  PASS here is that ruling layered over the machine verdict, not a
  contradiction of it.
- **New requirements surfaced for DES-068:** the live capture leg needs
  acoustic echo cancellation (open speakers without AEC produce a
  self-interruption feedback loop); seeds of 1KB/10KB/50KB were all
  accepted with no rejection, truncation, or connect penalty, and 1KB
  and 10KB answered crisply — but the observed 50KB response-quality
  degradation is CONFOUNDED by a harness turn-end bug (Bugbot, PR #481:
  the turn closed before slow-tool answers were delivered), so
  seed-quality limits need a clean re-test under DES-070's own
  validation (`vox-73y7`); EL agent deletion propagates lazily
  (teardown must force-delete, 404-idempotent).
- **Security copy-forward constraints for the `voxd` port** (from the
  spike's security review): the event-trace sink must redact
  token-shaped fields (`persistent_session_token`, `signed_url`,
  `*token*`, `*secret*`) before persisting any verbatim server body;
  signed conversation URLs are bearer credentials and must never appear
  in logs, traces, or exception text; the WebSocket client must cap
  `max_size` (the spike's unbounded setting is acceptable only against
  trusted EL in a local harness); live-session transcripts are not
  committed unscrubbed.
- Operator confirmed quality by ear: voice, latency, and turn-taking
  "incredibly better than what we did in earlier spikes."

---

## DES-070: Voice-Agent Context — `/vox:talk` Seed + Hook Fanout Rolling Store, Extending DES-067

**Status:** PROPOSED (2026-08-29). Sub-decision under DES-068. Validation: `vox-73y7`.

### Context

DES-067 Decision 1 established that the primary Claude Code session authors the call agent's opening context snapshot itself, folding in `quarry --json find` hits and any needed docs before the call agent spawns. That insight is correct and load-bearing; E+ retains it as the *seed* mechanism.

What DES-067 did not address, because DES-066's call agent was a persistent peer with its own read tools, is *how the voice agent stays current across a longer conversation*. Under E+'s "one Claude Code turn wraps N voice turns" shape (DES-068), the primary session is blocked in `/vox:talk` for the whole call and cannot itself update anything mid-conversation. Either the seed is sufficient at call start and stays static, or context has to arrive some other way.

### Proposed decision

Two-layer context feed:

**Layer 1 — `/vox:talk` seed.** The primary session constructs and passes a snapshot at call start, per DES-067 Decision 1 verbatim. Same authorship pattern, same quarry/context7 upstream fold-in. This is the load-bearing layer; if it is rich enough, Layer 2 becomes optional.

**Layer 2 — hook fanout as continuous context.** `voxd` retains payloads from every Claude Code hook that reaches it via `mcp-proxy --hook` for chime purposes (`SessionStart`, `PromptSubmit`, `PostToolUse`, `Stop`, `Notification`). It stamps a monotonic per-session sequence and stores the last N raw turns plus a running summary in an in-`voxd` rolling context store, keyed by session. The voice agent can consult this store via a `read_recent_conversation` client tool (DES-068's tool surface) mid-call.

For the primary session that just fired `/vox:talk` and is now blocked, Layer 2 is not adding new information *from that session* — the blocked session isn't firing hooks. But if the user has other Claude Code sessions active elsewhere (a common case on a distributed dev-box topology, and always the case in Mode B voice-first — DES-071), Layer 2 lets the voice agent see across them.

### Rejected alternatives

- **Seed-only, no rolling store.** Rejected as too rigid for calls longer than a few turns, and for Mode B voice-first entry (DES-071) where no primary session has yet run a `/vox:talk` seed. Retained as a fallback if `vox-73y7` finds hook payloads too thin to be worth retaining.
- **Live snapshot on demand — voice agent asks primary session for updated context via a tool call.** Rejected because the primary session is blocked in `/vox:talk` and cannot respond to tool calls during that turn.
- **Push updates from primary session into `voxd` via a side channel (not hooks).** Rejected as duplicating the hook fanout that already exists.

### Open items / risks

- Hook payload sufficiency (do `PostToolUse` payloads carry actionable state, or only metadata?) is unmeasured; `vox-73y7` addresses it directly.
- Sequence-gap detection under WAN drops is a real distributed-systems problem. Kill criterion: gap-detection fails to reliably catch drops **and** `/vox:talk` seed alone is not rich enough to make Layer 2 decorative — that is, the design is killed only when *neither* mitigation holds.
- Cross-session context (voice agent seeing turns from a *different* primary session than the one that fired `/vox:talk`) may or may not be desirable — treated as opt-in, defaulting off, per privacy and confusion concerns.

### Validation outcome (2026-08-31, `vox-73y7`)

**Hook fanout is load-bearing — Layer 2 earns its place, and the seed
built from it grades even better.** Full evidence:
`spikes/vox-73y7-hook-context/REPORT.md` and its committed capture
(ledger, graded reconstructions, latency and gap artifacts), all
evaluator-verified by independent recomputation and re-grading.

- **Payload sufficiency: PASS.** `PostToolUse` carries full tool
  inputs/outputs (tracebacks, file contents) — state p50 ≈1.5KB, max
  ≈11KB per event. "What was I just doing?" reconstructions from the
  raw ledger tail graded 4 PASS + 1 PARTIAL across five timepoints of a
  real debug-loop session; a curated ~10KB seed built from the same
  feed graded 5/5. Both context layers work; neither is decorative.
- **Delivery latency:** hook-fire to store-visible 32ms p50 / 43ms p95
  — effectively real-time for conversation purposes.
- **Design correction (must survive into implementation): sequence
  numbering belongs on the SENDER.** A real 9-event loss window was
  detected and quantified only by sender-side `relay_seq` stamped at
  the hook relay; receiver-side stamping — this entry's original
  wording — provably cannot see loss (never-received events leave no
  receiver-side holes) and collides across store restarts. The rolling
  store's gap detection must consume sender-assigned sequences. Two
  blind spots come with that mechanism and must be designed for:
  losses BEFORE the sender's counter increments (a crashed relay
  wrapper drops the event with no sequence ever assigned — the relay
  must fail the hook loudly rather than exit clean), and TRAILING
  losses after the last received event (invisible to sequence
  comparison; pair with an end-of-session handshake or accept and
  document the gap).
- **Seed-size ceiling revised up:** the 50KB quality degradation
  recorded under vox-bst7 does NOT reproduce under the fixed harness
  (3 bounded EL sessions, frozen bst7 harness byte-identical to main)
  — it was the harness turn-end bug. Large seeds are viable; ~10KB
  remains the curated sweet spot because it grades 5/5, not because
  bigger breaks.

---

## DES-071: Mode B Voice-First Entry — User Talks First, `voxd` Launches a Fresh Session Same-Host by Default

**Status:** PROPOSED (2026-08-29). Sub-decision under DES-068, extending it with a second entry point. Validation: `vox-juhw`.

### Context

DES-068's default entry is Mode A — an existing primary Claude Code session invokes `/vox:talk`. That covers the "I'm in the middle of something and want to think out loud" case. It does not cover the "I want to start working on X, by voice, from scratch" case, where the user opens the mic before any coding session exists — increasingly plausible on the distributed dev-box topology where the user is often not in a terminal at all when they decide to start work.

The natural shape: the user speaks to `voxd` directly (wake word, `vox call` from a terminal, hotkey). `voxd` opens an EL Conv AI session cold — no seed from a primary session that doesn't exist — and the voice agent, mid-conversation, decides (or the user asks) to spin up a coding session. That launch is a tool call.

### Proposed decision

`voxd` exposes `launch_session(agent, task, host?, permissions_profile?)` as a client tool to the voice agent:

- `agent` — one of `"claude"`, `"pi"`, `"opencode"`.
- `task` — an initial prompt derived from the voice conversation up to that point.
- `host` — optional. If omitted, `voxd` forks the agent locally as a `subprocess.Popen` inside a detached `tmux` session on its own host; that is **v1**. If supplied, `voxd` resolves it against a registered-hosts config and `ssh`-execs the same shape onto the named host; that is **v2**.
- `permissions_profile` — names a curated set of allowed tools, MCP server subset, and permission mode that the spawned session inherits. The launch is not a capability handoff to a fully-empowered fresh agent unless the profile says so.

v1 needs no SSH, no host registry, no auth. Loopback hook fanout from the spawned session's own `mcp-proxy --hook` reaches `voxd` without WAN or credentials, feeding DES-070's rolling context store in real time. v2 uses the same TLS+bearer path Mode A uses for its hooks.

At call end, the transcript (and any `write_note` outputs) return as the `/vox:talk`-equivalent tool call's value — same handshake as Mode A. If the spawned session is still running, it stays alive under `tmux`; the user attaches with `tmux attach -t <name>` when convenient.

### Rejected alternatives

- **Mode A only; require the user to open a Claude Code session first, then call `/vox:talk`.** Rejected as failing the "start work by voice" case, which is real for the distributed dev-box topology.
- **SSH-remote as v1 default, same-host as fallback.** Rejected on complexity: v2's registered-hosts config and SSH-agent story is real ceremony; getting v1 right first (voxd forks locally) settles the tool signature so v2 is an implementation add-on, not an API change.
- **No launch capability; voice agent only takes notes and the user starts the session by hand afterward.** The most conservative shape. Rejected because it makes voice-first mode a research/planning tool only, not a way to kick off work — losing most of Mode B's leverage.

### Open items / risks

- `tmux attach` UX for the spawned session mid-run is untested end-to-end; `vox-juhw` verifies the fork → configure → attach → hook-loopback chain.
- The concrete shape of `permissions_profile` (what named profiles ship, how they compose, how they surface to the voice agent for informed selection) is not fully specified here; it inherits from Claude Code's own permission model and gets worked out in the implementation mission.
- v1's "user's project must live on the laptop" limitation is real: `voxd` on the laptop forks locally, so the spawned session sees the laptop's filesystem. For distributed dev-box users, v2 becomes non-optional. Documented as a known Mode B v1 scope, not a bug.

### Validation outcome (2026-08-30, `vox-juhw`)

The chain works: **fork → configure → attach → hook-loopback, end-to-end,
with zero custom Claude Code changes** — Mode B v1 is launcher engineering,
not a quarter. Full evidence: `spikes/vox-juhw-mode-b-launch/REPORT.md`
and its committed run artifacts (hook ledger, mid-run and post-kill pane
captures, teardown logs).

- A deposited project `.claude/settings.json` (`voice-launch-v1`) is a
  workable permissions-profile mechanism: honored with zero flags and
  zero prompts. **Precision that must survive into implementation:**
  `acceptEdits` confines *edits* to the project; reads are NOT
  path-confined in v1 — a launched fork can read anything the launching
  user can, including its seeded credentials file. Mitigation is
  path-scoped permission rules; launcher-side, no Claude Code change.
- Hooks from the spawned session reach the store over the real
  `mcp-proxy --hook`, ordered and session-attributed; killing `voxd`
  (the store) does not orphan or kill the spawned session — it keeps
  working and stays attachable, with hook relays failing non-blocking.
- Eight rough edges recorded in the REPORT, all launcher/config work:
  trust-dialog pre-seed, credential seeding + the credential-read
  surface, env hygiene (blank inherited API keys), process-group kill
  discipline, scratch placement, readiness signaling, login expiry.
- Implementation requirements carried forward: the real `launch_session`
  capability must itself enforce scratch-namespace placement
  (deny-by-default in the capability, not the harness), and DES-070's
  context store needs recursive redaction of nested payloads.

---

## DES-073: Steering a Running Mode B Session — Per-Agent Channels, Amending DES-068's Wall 2

**Status:** RATIFIED 2026-08-31 (operator: "If the results were good, we
can ratify the design" — results verified good: both arms PASS,
evaluator-accepted round 1, twelve local-review findings fixed with the
evidence regenerated on the hardened harness). Sub-decision under
DES-068, amending its "Wall 2" premise. Validation: `vox-04qy` (evidence
and per-arm semantics in `spikes/vox-04qy-steering/REPORT.md`).
Implementation may dispatch against this design.

### Context

DES-068 framed the option space with "Wall 2 — no background process can
inject: Claude Code exposes no channel by which an external process hands
a new user turn in." That premise ruled out steering — the user speaking
to `voxd` mid-run and having those words reach the working session. The
`vox-04qy` spike tested the two candidate channels directly and both
delivered, with confirmed receipts and characterized semantics.

### Proposed decision

Amend Wall 2 from "no injection is possible" to "no injection *API*
exists; the TTY is a workable injection surface, and protocol-native
agents have first-class verbs." Steering becomes a `voxd` capability with
a per-agent channel:

- **pi** — the RPC `steer` verb over the daemon-held process's stdin.
  Millisecond acks; queue-at-boundary semantics (the in-flight tool call
  always completes; the text enters as a user message at the next turn
  boundary). `follow_up` contrasts as end-of-task delivery for deferred
  instructions.
- **claude** — tmux `send-keys` into the harness-owned pane (exactly what
  Mode B's launcher already creates), with the DES-070 hook store as the
  delivery receipt: the injected text's own `UserPromptSubmit` event is
  the confirmation. Same queue-then-inject shape (shared `prompt_id`
  with the running turn; incorporated at the model's next inference
  boundary). Escape is the hard interrupt — and it fires **no Stop
  hook**, so a steering daemon must not use Stop as its turn-ended
  signal.
- **Steer text must be user-voiced** on every channel. Delivery and
  compliance are separate: protocol-styled wrapper text ("URGENT STEER")
  was delivered in milliseconds and refused by the model as suspected
  prompt injection in one run (nondeterministically — the identical text
  complied on a rerun). `voxd` relays the user's words as the user's
  words, never wrapped in machinery.

### Codex / opencode disposition (operator-ruled 2026-08-31)

Two datapoints suffice; no third spike arm. The spike proved the two
channel *types* — a native protocol verb and the TTY — and every coding
agent has one or both (opencode is client/server with an API surface;
codex ships programmatic modes). When `launch_session` grows an agent,
that adapter's implementation includes characterizing its best steering
channel with the committed `vox-04qy` harness. One genuine per-agent gap
to carry: the hook-store receipt is Claude-specific — other agents need
their own delivery witness (pane capture, or their server/event surface).

### Rejected alternatives

- **Steering only at turn boundaries (no mid-turn delivery).** Rejected:
  both channels already queue safely mid-turn; refusing mid-turn sends
  buys nothing and costs the "stop, wrong file" case, where Escape (claude)
  or `abort` (pi) plus a user-voiced correction is the whole point.
- **A Claude Code change to add an injection API.** Rejected for v1: the
  TTY channel works today with zero Claude Code changes, the same
  property that made Mode B v1 shippable (DES-071).
- **Wrapping steer text in a protocol envelope for machine parsing.**
  Rejected: the Arm 1 adversarial run showed wrapper-styled text risks
  model refusal as suspected injection; the channel carries user words.

### Open items / risks

- Model compliance is phrasing- and model-dependent and nondeterministic;
  the channel guarantees delivery, not obedience. UX must not promise
  "the agent will do what you said," only "the agent heard you."
- No per-token stream on the claude channel — injection timing inside a
  single inference call is unobservable from outside.
- The store appends: any consumer re-running capture over an existing
  ledger would interleave sessions (guarded in the spike harness;
  the real capability needs the same refusal).
