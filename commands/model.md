---
description: "Switch TTS model (no arg opens a picker)"
argument-hint: "[model-name]"
allowed-tools: ["mcp__plugin_vox_mic__model", "AskUserQuestion"]
---

# /vox:model command

Switch the TTS model for the current provider mid-session. No argument opens
a picker; a name (full or shorthand) writes it to the session and to
`.punt-labs/vox/vox.md`.

## Usage

- `/vox:model` — pick from the models the current provider offers
- `/vox:model v3` — set model to `eleven_v3` (elevenlabs shorthand)
- `/vox:model eleven_flash_v2_5` — set by full name

## Implementation

First normalize `$ARGUMENTS`: trim whitespace. An empty string counts as no
argument.

- **(no argument)**:
  1. Call the `model` MCP tool with no arguments to get `{"available": [...],
     "current": "..."}` for the currently selected provider.
  2. If `available` is empty, tell the user
     `"No models to pick for this provider."` and stop.
  3. Otherwise call `AskUserQuestion` with one question:
     - `question`: `"Which model?"`
     - `header`: `"Model"`
     - `multiSelect`: false
     - `options`: one per name in `available`; the current selection's
       `description` is `"(current)"`, others `""`.
  4. On the user's pick, call `model` again with `name=<pick>`. No text
     output — the panel confirms.
- **`<name>`**: Call the `model` MCP tool with `name="<name>"` directly.
  Elevenlabs shorthand (`v3`, `flash`, `turbo`, `multilingual`) resolves
  server-side. No text output.
