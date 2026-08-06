---
description: "Switch TTS model/provider, or enable/disable vox in this repo"
argument-hint: "model <name> | provider <name> | enable | disable"
allowed-tools: ["mcp__plugin_vox_mic__unmute", "mcp__plugin_vox_mic__enablement"]
---

# /vox command

Switch the TTS model or provider mid-session, or turn vox on/off for the repo.
The notification level (task-completion vs continuous) is a per-repo config, set
with `vox notify normal|continuous` from a shell.

## Usage

- `/vox model <name>` — switch TTS model (e.g. `v3`, `flash`, `turbo`)
- `/vox provider <name>` — switch TTS provider (e.g. `elevenlabs`, `openai`, `polly`, `say`)
- `/vox enable` — turn vox on for this repo
- `/vox disable` — turn vox off for this repo

## Implementation

Parse `$ARGUMENTS`:

### `model <name>`

Resolve the model shorthand to full model ID:

- `v3` → `eleven_v3`
- `flash` → `eleven_flash_v2_5`
- `turbo` → `eleven_turbo_v2_5`
- `multilingual` → `eleven_multilingual_v2`
- Anything else → pass through as-is (e.g. `tts-1`, `tts-1-hd`)

Call the `unmute` MCP tool with only the `model` parameter (no text). Confirm: "Switched model to `<full_id>`."

### `provider <name>`

Call the `unmute` MCP tool with only the `provider` parameter (no text). When switching providers, also pass `model` as empty string to clear the previous provider's model from config. Confirm: "Switched provider to `<name>`."

Valid providers: `elevenlabs`, `openai`, `polly`, `say`, `espeak`.

### `enable`

Turn vox on for the current repository: deposit the guide, write the
`.punt-labs/vox/enabled` marker, add the `@`-import to `CLAUDE.md`, and register
the repo settings. Idempotent — re-running upgrades the deposited guide and adds
no second import.

Call the `enablement` MCP tool with `action="enable"`. Read the JSON reply and
confirm in one line, e.g. "vox enabled in `<repo>`." The marker is a working-tree
change — remind the user to commit it via a PR (neither surface runs git).

### `disable`

Turn vox off for the current repository: remove the `@`-import from `CLAUDE.md`,
delete the `.punt-labs/vox/enabled` marker, and deregister the repo settings. The
`.punt-labs/vox/` subtree is left dormant (non-destructive) — to remove it too,
run `vox disable --purge` from a shell.

Call the `enablement` MCP tool with `action="disable"`. Read the JSON reply and
confirm in one line, e.g. "vox disabled in `<repo>`." The change is a working-tree
edit — remind the user to commit it via a PR.

### No argument or unrecognized

Tell user: "Usage: `/vox model <name>`, `/vox provider <name>`, `/vox enable`, or `/vox disable`; for the notification level use `vox notify normal|continuous`."
