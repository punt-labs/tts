---
description: "Chimes only — disable spoken notifications"
allowed-tools: ["mcp__plugin_vox_mic__speak"]
---

# /mute command

Disable spoken notifications. Chime sounds still play on task completion and permission prompts.

## Implementation

Call the `speak` MCP tool with `mode="n"`. Then reply with one of these (pick at random, don't repeat the last one used):

- "OK, I'll zip it."
- "Going silent."
- "Lips sealed."
- "Say no more."
- "Chimes only, got it."
- "Muting the commentary."
- "Read you loud and clear. Well — just clear."
