---
description: "Enable vox in this repo"
allowed-tools: ["mcp__plugin_vox_mic__enablement"]
---

# /enable command

Turn vox on for the current repository: deposit the guide, write the
`.punt-labs/vox/enabled` marker, add the `@`-import to `CLAUDE.md`, and register
the repo settings. Idempotent — re-running upgrades the deposited guide and adds
no second import.

## Implementation

Call the `enablement` MCP tool with `action="enable"`. Read the JSON reply and
confirm in one line, e.g. "vox enabled in `<repo>`." The marker is a working-tree
change — remind the user to commit it via a PR (neither surface runs git).
