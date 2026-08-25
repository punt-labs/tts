---
description: "Enable voice mode (spoken notifications)"
allowed-tools: ["mcp__plugin_vox_mic__speak"]
---

# /unmute command

Enable voice mode: spoken notifications turn on. Does not change the
notification level — use `mic:notify` / `vox notify` for that. To set the
session voice, use `/vox:voice`.

## Usage

- `/unmute` — Enable voice mode (spoken notifications)

## Implementation

Call the `speak` MCP tool with `mode="y"`. No text output — the panel confirms.
