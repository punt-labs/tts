---
description: "Set session voice (no arg opens a picker)"
argument-hint: "[voice-name]"
allowed-tools: ["mcp__plugin_vox_mic__voice", "AskUserQuestion"]
---

# /vox:voice command

Set the session voice. No argument opens a picker from the current provider's
featured voices; a name (with an optional stray leading `@` stripped) writes
it to the session and to `.punt-labs/vox/vox.md`.

## Usage

- `/vox:voice` — pick from the featured voices for the current provider
- `/vox:voice matilda` — set session voice to `matilda`
- `/vox:voice @matilda` — the `@` is stripped (a common typo)

## Implementation

First normalize `$ARGUMENTS`: strip a leading `@` if present (a user may type
`@matilda` out of habit) and trim whitespace. Treat a lone `@` or an empty
string as no argument.

- **(no argument)**:
  1. Call the `voice` MCP tool with no arguments to get
     `{"provider", "current", "available", "featured"}`.
  2. If `featured` is empty or has fewer than 2 entries, call `voice` with
     `name=<current>` (or just skip the picker) and stop.
  3. Otherwise build a candidate list capped at 4: start with `current` (if
     set), then append entries from `featured`, de-duplicating by name.
     Call `AskUserQuestion` with one question:
     - `question`: `"Which voice?"`
     - `header`: `"Voice"`
     - `multiSelect`: false
     - `options`: `label=<name>`, `description=<blurb-from-featured>`; the
       current selection's description is suffixed `" (current)"`.
  4. On the user's pick, call `voice` again with `name=<pick>`. No text
     output — the panel confirms.
- **`<name>`**: Call the `voice` MCP tool with `name="<name>"` directly. The
  server strips a stray `@` sigil and writes the normalized name. No text
  output.
