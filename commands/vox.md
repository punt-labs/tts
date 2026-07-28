---
description: "Switch TTS model or provider mid-session"
argument-hint: "model <name> | provider <name>"
allowed-tools: ["mcp__plugin_vox_mic__unmute"]
---

# /vox command

Switch the TTS model or provider mid-session.

Enablement moved to its own commands: use `/enable` and `/disable` to turn vox on
and off for a repo. The notification level (task-completion vs continuous) is a
per-repo config, set with `vox notify normal|continuous` from a shell.

## Usage

- `/vox model <name>` — switch TTS model (e.g. `v3`, `flash`, `turbo`)
- `/vox provider <name>` — switch TTS provider (e.g. `elevenlabs`, `openai`, `polly`, `say`)

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

### No argument or unrecognized

Tell user: "Usage: `/vox model <name>` or `/vox provider <name>`. To turn vox on/off use `/enable` / `/disable`; for the notification level use `vox notify normal|continuous`."
