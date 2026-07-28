---
description: "Disable vox in this repo"
allowed-tools: ["mcp__plugin_vox_mic__enablement"]
---

# /disable command

Turn vox off for the current repository: remove the `@`-import from `CLAUDE.md`,
delete the `.punt-labs/vox/enabled` marker, and deregister the repo settings. The
`.punt-labs/vox/` subtree is left dormant (non-destructive) — to remove it too,
run `vox disable --purge` from a shell.

## Implementation

Call the `enablement` MCP tool with `action="disable"`. Read the JSON reply and
confirm in one line, e.g. "vox disabled in `<repo>`." The change is a working-tree
edit — remind the user to commit it via a PR.
