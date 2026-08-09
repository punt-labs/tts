---
description: "Enable or disable vox in this repo"
argument-hint: "enable | disable"
allowed-tools: ["mcp__plugin_vox_mic__enablement"]
---

# /vox command

Turn vox on or off for the current repository. The three mid-session switches
— model, provider, and voice — each live on their own top-level slash
command now (`/vox:model`, `/vox:provider`, `/vox:voice`), so `/vox` is
reserved for enablement. The notification level (task-completion vs
continuous) is a per-repo config, set with `vox notify normal|continuous`
from a shell.

## Usage

- `/vox enable` — turn vox on for this repo
- `/vox disable` — turn vox off for this repo

## Implementation

Parse `$ARGUMENTS`:

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

Tell user: "Usage: `/vox enable` or `/vox disable`. To switch model, provider, or voice mid-session, use `/vox:model`, `/vox:provider`, or `/vox:voice`. For the notification level use `vox notify normal|continuous`."
