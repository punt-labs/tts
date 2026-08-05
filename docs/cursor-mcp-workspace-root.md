# Cursor MCP and Git-Root Servers

Issue description and required server-side fix for repo-scoped MCP servers (biff, quarry, ethos, etc.) when run under Cursor IDE.

## Summary

Punt-labs plugins register stdio MCP servers that must discover the **git repository root** at startup. This works in Claude Code and fails in Cursor because the two hosts use different spawn contracts. Cursor does not document a single, reliable way for **plugin-provided** MCP to receive workspace context. The durable fix belongs in each MCP server (and in a shared punt-labs convention), not in per-user `.cursor/mcp.json` bash wrappers.

## Problem

### Symptom

In Cursor **Settings → Tools & MCP → Plugin MCP Servers**, `tty` (biff) shows **Error** with:

```text
Not in a git repository. Run biff from inside a repo.
```

Other punt-labs plugin MCP servers (`mic`, `self`, `email`, etc.) may appear connected but are spawned the same way; any server that resolves paths from cwd at startup is affected when cwd is wrong.

### What biff needs the repo for

At MCP startup, biff calls `find_git_root()` (default: walk up from `Path.cwd()`) and uses that path to:

- Load per-repo config (`.punt-labs/biff/config.yaml`, ethos identity)
- Compute per-repo state directory (`{prefix}/biff/{repo_name}/`)
- Determine whether biff is enabled for this repo
- Scope team presence and messaging to this project

This is intentional product design. Biff is repo-scoped team communication, not a global daemon.

### What the plugin declares

From biff `plugin.json`:

```json
"mcpServers": {
  "tty": {
    "type": "stdio",
    "command": "biff",
    "args": ["mcp"]
  }
}
```

The implied contract: **the host spawns the process with cwd (or equivalent) set to the open workspace’s git root.** Unix CLI convention — same as running `bd ready` or `git status` from the project directory.

### What Claude Code does

[Claude Code MCP documentation](https://code.claude.com/docs/en/mcp) states:

- Claude Code sets **`CLAUDE_PROJECT_DIR`** in the spawned MCP server’s environment to the project root.
- Plugin MCP configs may substitute `${CLAUDE_PROJECT_DIR}` in command/args.
- Servers may also call MCP **`roots/list`** to obtain the launch directory.

Biff does not currently read `CLAUDE_PROJECT_DIR`; it still works in Claude Code because **cwd is typically the project root** when the session is opened on a repo.

### What Cursor does

Observed behavior (Cursor 2026-06, macOS):

| Source | Behavior |
|--------|----------|
| **Plugin MCP** | Registered from marketplace plugin manifest. Shown under **Plugin MCP Servers** with short names (`tty`, `mic`, …). Spawned **without** cwd set to the workspace git root. |
| **Project `.cursor/mcp.json`** | Loaded as internal IDs like `project-0-vox-*`. Can override spawn command. Often starts **disconnected** if toggled off; not clearly labeled “Project” in Settings UI. |
| **User `~/.cursor/mcp.json`** | Shown under **User MCP Servers**. Spawns in **every Cursor window**, including windows with no workspace — causes intermittent failures when `${workspaceFolder}` is empty or `null`. |
| **Multi-window** | Two windows race to start the same User MCP server; one succeeds (vox open), one fails (no workspace). Settings may show **Error** from the failing window even when the agent window connected. |

[Cursor MCP docs](https://cursor.com/docs/mcp) document:

- Project config: `.cursor/mcp.json` in repo root
- Global config: `~/.cursor/mcp.json`
- Variable interpolation in `command`, `args`, `env`, `url`, `headers`: `${workspaceFolder}`, `${userHome}`, `${env:NAME}`
- MCP **Roots** supported at protocol level
- Official stdio table: `type`, `command`, `args`, `env`, `envFile` — **no documented `cwd` field** (community reports `cwd` works in practice; not guaranteed)

[Cursor forum, staff answer May 2026](https://forum.cursor.com/t/how-do-we-configure-workspace-specific-plugin-mcp-servers/161660):

> Plugin config variables are **global** — no per-workspace scoping. Add the server in each project’s **`.cursor/mcp.json`** and **disable the plugin copy** in Settings.

So: **plugin MCP cannot be configured per-repo through the plugin system.** Workarounds are project-level `mcp.json` duplicates or fixing the server to not depend on host cwd.

### Variable expansion quirks (Cursor)

Documented variables do not always behave as expected inside shell wrappers:

| Variable | Expected | Observed |
|----------|----------|----------|
| `${workspaceFolder}` | Absolute path to project root | Sometimes literal `null` or empty in windows without a workspace |
| `~/path` in bash `cd` | Home expansion | **Not expanded** by bash when passed as literal `~/…` |
| `${userHome}` in bash `-c` string | `/Users/…` | Sometimes **not interpolated** → path becomes `/.cursor/…` |

These quirks make bash-wrapper overrides fragile and user-specific.

## Root cause (one sentence)

**Repo-scoped MCP servers assume the host provides workspace context (cwd or env); Claude Code does; Cursor plugin MCP does not consistently; biff only discovers repo via cwd.**

## Why `.cursor/mcp.json` wrappers are not the solution

Attempts in this session (bash `cd`, wrapper scripts, global vs project config, `repo-*` renaming) demonstrated:

1. **Wrong layer** — fighting Cursor spawn semantics per-user, per-window, not fixing the plugin/server contract.
2. **Wrong scope** — global User MCP runs in all windows; project MCP is hidden/disconnected in UI.
3. **Fragile** — depends on `${workspaceFolder}` interpolation, multi-window race, and shell edge cases.
4. **Not portable** — every punt-labs repo needs duplicate config; teammates must manually disable plugin copies in Settings.

Cursor staff’s endorsed workaround (project `mcp.json` + disable plugin) is operational but duplicates servers and does not scale across the org.

## Required fix: MCP server workspace resolution

Each repo-scoped punt-labs MCP server should resolve the git root through a **ordered, documented chain** — not cwd alone.

### Resolution order (proposed punt-labs convention)

```text
1. Explicit CLI flag          --repo / --start (already supported internally via `start=` in load_mcp_config)
2. Host env: Claude Code      CLAUDE_PROJECT_DIR
3. Host env: Cursor           CURSOR_PROJECT_DIR (proposed — see below)
4. Host env: generic          MCP_WORKSPACE_ROOT or PUNT_REPO_ROOT (user override)
5. MCP protocol               roots/list (if client supports Roots — Cursor docs say yes)
6. Fallback                   find_git_root(Path.cwd())
7. Fail                       clear error naming host and env vars to set
```

### 1. Read host environment variables

**Claude Code (documented today):**

```python
import os
from pathlib import Path

def _host_project_dir() -> Path | None:
    for key in ("CLAUDE_PROJECT_DIR", "CURSOR_PROJECT_DIR", "MCP_WORKSPACE_ROOT", "PUNT_REPO_ROOT"):
        raw = os.environ.get(key)
        if raw:
            return Path(raw).expanduser().resolve()
    return None
```

**Cursor:** As of 2026-06, Cursor does **not** document setting `CLAUDE_PROJECT_DIR` or an equivalent when spawning plugin MCP. Servers should still read `CLAUDE_PROJECT_DIR` for Claude Code parity and define **`CURSOR_PROJECT_DIR`** (or adopt **`MCP_WORKSPACE_ROOT`**) as a cross-host name punt-labs controls via project `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "biff": {
      "type": "stdio",
      "command": "biff",
      "args": ["mcp"],
      "env": {
        "MCP_WORKSPACE_ROOT": "${workspaceFolder}"
      }
    }
  }
}
```

Interpolation of `${workspaceFolder}` into **`env`** is documented by Cursor and avoids bash `-c` fragility.

Plugin MCP cannot set per-project `env` today; therefore **biff must not rely on plugin MCP alone in Cursor** until either (a) Cursor sets a standard env var for plugin spawns, or (b) biff implements roots/list.

### 2. Implement MCP `roots/list` (protocol-level)

[Cursor MCP docs](https://cursor.com/docs/mcp) list **Roots** as supported. MCP servers that need a workspace boundary should, during initialization:

1. Call **`roots/list`** on the client.
2. Use the first `file://` root as `start` for `find_git_root()`.
3. Fall back to cwd only if roots are empty or unsupported.

This is the host-agnostic fix that does not depend on Cursor’s JSON config or bash wrappers. It matches how MCP is supposed to communicate workspace boundaries.

Pseudocode:

```python
async def resolve_repo_root(client, start: Path | None) -> Path:
    if start is not None:
        root = find_git_root(start)
    else:
        host_dir = _host_project_dir()
        root = find_git_root(host_dir) if host_dir else None

    if root is None and client supports_roots:
        roots = await client.list_roots()
        for uri in roots:
            if uri.scheme == "file":
                root = find_git_root(Path(uri.path))
                if root:
                    break

    if root is None:
        root = find_git_root()  # cwd fallback

    if root is None:
        raise SystemExit(
            "Not in a git repository. Open a git repo in the IDE, set MCP_WORKSPACE_ROOT, "
            "or run from inside a repo."
        )
    return root
```

### 3. Wire into biff

In `biff.config._load_base_config` / `load_mcp_config`:

- Before `find_git_root(start)`, set `start = start or _host_project_dir()`.
- In `biff mcp` startup, after creating the FastMCP server but before `load_mcp_config`, obtain roots from the MCP client if available (may require splitting init so roots are fetched post-connect — design detail for biff implementer).

Optional: add Typer `--start` / `--repo` to `biff mcp` so project `.cursor/mcp.json` can pass `"args": ["mcp", "--repo", "${workspaceFolder}"]` without shell wrappers (interpolation in args is documented).

### 4. Plugin manifest (secondary)

Once server-side resolution exists, plugin `plugin.json` can stay minimal:

```json
"command": "biff",
"args": ["mcp"]
```

For Claude Code, optionally document `${CLAUDE_PROJECT_DIR}` in plugin args if biff reads env before cwd. No bash, no wrapper scripts.

## Cursor user workaround (until servers are fixed)

Per [Cursor staff guidance](https://forum.cursor.com/t/how-do-we-configure-workspace-specific-plugin-mcp-servers/161660):

1. Add to **`vox/.cursor/mcp.json`** (project only — not global):

   ```json
   {
     "mcpServers": {
       "biff": {
         "type": "stdio",
         "command": "biff",
         "args": ["mcp"],
         "env": {
           "MCP_WORKSPACE_ROOT": "${workspaceFolder}"
         }
       }
     }
   }
   ```

2. **Disable** plugin `tty` under Plugin MCP Servers.
3. Run **one** Cursor window opened on the repo (avoid multi-window User MCP races).
4. Optionally try undocumented `"cwd": "${workspaceFolder}"` if `env` alone is insufficient before biff reads `MCP_WORKSPACE_ROOT`.

This is a stopgap, not the org-wide fix.

## Scope: which punt-labs servers need this

| Server | Repo-scoped at MCP start? | Priority |
|--------|---------------------------|----------|
| **biff** (`tty`) | **Yes** — hard fail without git root | P0 |
| **quarry** | Likely — indexes project | P1 |
| **ethos** (`self`) | Likely — repo identity | P1 |
| **vox** (`mic`) | Soft — `.punt-labs/vox/` from cwd | P2 |
| **beadle, lux, grimoire, zspec** | Lower / plugin-path based | P3 |

Shared helper recommended: `punt_kit.mcp.workspace` or `biff._stdlib.resolve_repo_root()` extracted for reuse.

## Acceptance criteria

Fixed when **all** of the following hold:

1. Plugin `tty` enabled in Cursor, vox repo open, **no** project `.cursor/mcp.json` override → biff MCP connects green.
2. Multi-window: no Error from spurious non-workspace windows (server or host handles null context gracefully).
3. Claude Code behavior unchanged (still uses `CLAUDE_PROJECT_DIR` / cwd).
4. Clear error if no repo context from any source (message mentions MCP roots and env vars).

## References

- [Cursor MCP docs](https://cursor.com/docs/mcp)
- [Cursor: workspace-specific plugin MCP (staff answer)](https://forum.cursor.com/t/how-do-we-configure-workspace-specific-plugin-mcp-servers/161660)
- [Cursor: `${workspaceFolder}` feature request / `.` arg tip](https://forum.cursor.com/t/allow-workspacefolder-in-mcp-project-configration/74861)
- [Claude Code MCP — `CLAUDE_PROJECT_DIR`](https://code.claude.com/docs/en/mcp)
- Biff: `load_mcp_config()` / `_load_base_config()` in `biff/src/biff/config.py`
- Biff plugin: `.claude-plugin/plugin.json` → `biff mcp`

## Tracking

Suggested beads:

- **biff-???** — Implement workspace resolution chain (env + MCP roots + `--repo` flag)
- **punt-kit-???** — Document `MCP_WORKSPACE_ROOT` convention for all punt-labs MCP servers
- **cursor-???** — Upstream: request Cursor set `CURSOR_PROJECT_DIR` (or cwd) for plugin MCP spawns
