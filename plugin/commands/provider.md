---
description: "Switch TTS provider (no arg opens a picker)"
argument-hint: "[provider-name]"
allowed-tools: ["mcp__plugin_vox_mic__provider", "AskUserQuestion"]
---

# /vox:provider command

Switch the TTS provider mid-session. No argument opens a picker; a name from
the closed enum (`elevenlabs`, `openai`, `polly`, `say`, `espeak`) writes it
to the session and to `.punt-labs/vox/vox.md`.

## Usage

- `/vox:provider` — pick from the five providers
- `/vox:provider openai` — switch to OpenAI TTS

## Implementation

First normalize `$ARGUMENTS`: trim whitespace. An empty string counts as no
argument.

- **(no argument)**:
  1. Call the `provider` MCP tool with no arguments to get
     `{"available": [...], "current": "..."}`. `available` is the closed
     five-provider list — no daemon call needed.
  2. Call `AskUserQuestion` with one question:
     - `question`: `"Which provider?"`
     - `header`: `"Provider"`
     - `multiSelect`: false
     - `options`: one per name in `available`; the current selection's
       `description` is `"(current)"`, others `""`.
  3. On the user's pick, call `provider` again with `name=<pick>`. No text
     output — the panel confirms.
- **`<name>`**: Call the `provider` MCP tool with `name="<name>"` directly.
  The Literal schema rejects an unknown name before dispatch. No text output.
