# vox enable / disable + install.sh `--no-plugin`

Design for two related conformance items, shipped as one PR:

- **`punt-kit/standards/tool-enable-disable.md`** — vox gains per-repo
  `enable`/`disable` verbs at both surfaces (CLI + Claude Code), keyed on the
  `.punt-labs/vox/enabled` marker, retiring the `/vox y|n` toggle.
- **`punt-kit/standards/install-cli-only.md`** — `install.sh` gains
  `--no-plugin` / `VOX_NO_PLUGIN=1` to install the CLI without the Claude Code
  plugin.

The standards are binding; we comply unless a strong reason not to (there is
none here). This is a state-machine change (the marker's three states and the
hook-gate transitions), so it carries a `fuzz`-clean Z model before
implementation.

## 1. Why together, and the intersection

A CLI-only install (`--no-plugin`) has no plugin surface — no `/vox` slash
commands, no `mic` MCP tools, no plugin session hooks. Such a user still turns a
repo on with the **CLI** `vox enable`. The load-bearing intersection is a
**content** requirement, not just plumbing:

> When a `--no-plugin` user runs `vox enable`, the deposited
> `.punt-labs/vox/CLAUDE.md` — imported into the repo's `CLAUDE.md` — must give
> the agent enough guidance to drive vox through the **`vox` CLI** when
> appropriate, because the CLI is the only surface it has.

Today that guide is MCP/slash-centric; a plugin-less agent reads it and reaches
for `mic:unmute`, which is absent. So the guide must be **surface-aware**: use
the `mic` MCP tools if present, otherwise drive vox through the `vox` CLI. The
standard deposits one static guide at both scopes (§2.5), so a single file must
serve both the plugin agent and the CLI-only agent. Authoring that guide is a
first-class deliverable, and "a `--no-plugin` box that runs `vox enable` yields a
working CLI-driving agent" is an explicit acceptance scenario.

The two layers are orthogonal but composable:

- `--no-plugin` decides whether the **plugin layer** (slash, MCP, session hooks)
  is installed.
- `vox enable` decides whether **this repo** is on — the marker, the guidance
  import, and (gating on the marker) whether vox's hooks fire here.

`vox enable` (the CLI verb) is therefore **plugin-independent** — pure file
operations (deposit guide, write marker, edit the `CLAUDE.md` import, edit
`.claude/settings.json`). It never routes through the MCP. The `/vox enable`
(Claude Code) surface needs the plugin; on a `--no-plugin` box there is simply
one door instead of two, which §2.14 allows (both doors write the same marker).

## 2. The `/vox y|n` correction

`/vox y|n|c` is **per-repo** (it writes the repo's vox config), not per-session.
A per-repo on/off expressed as `y|n` is exactly what §2.3 retires, so it folds
directly into `enable`/`disable`:

- `/vox y` (vox on for this repo) → `vox enable`
- `/vox n` (off) → `vox disable`
- `/vox c` (continuous) is a **level within "on"**, not a third enablement
  state → a small per-repo config setter `vox notify normal|continuous`,
  separate from the marker. Enablement is the marker; the level is config.

The earlier "keep it as a per-user `y|n` layer (§2.14)" idea is rejected: that
applies to genuinely per-*user* preferences (biff's `mesg`), and vox's notify
setting is per-*repo*.

## 3. Design

### 3.1 The `enabled` marker — three states (§2.7)

The tool is enabled in a repo iff `<repo>/.punt-labs/vox/enabled` exists. The
marker is committed (per-repo policy, reviewable in a PR), carved out of the
vendored-zone overwrite but not gitignored.

| State | `.punt-labs/vox/` dir | `enabled` marker | import line in `CLAUDE.md` |
|-------|-----------------------|------------------|----------------------------|
| Enabled | present | present | present |
| Dormant | present | absent | absent |
| Absent | absent | absent | absent |

**Invariant (§2.11):** `enabled` marker present ⟺ the repo `CLAUDE.md` contains
exactly one `@.punt-labs/vox/CLAUDE.md` line. This biconditional is the core
property the Z model must preserve across `enable` and `disable`.

### 3.2 Two surfaces, one marker (§2.14)

| Surface | Form | Writes |
|---------|------|--------|
| CLI | `vox enable` / `vox disable`, run in the repo | the marker, guide, import, settings |
| Claude Code | `/vox enable` / `/vox disable` (via `mic` MCP `action`) | the same marker |

The MCP tool takes `action: "enable" | "disable"` — never `enabled: bool`
(§2.14). Neither surface runs git; the marker is a working-tree change committed
via PR. No auto-enable (§2.3): vox never turns itself on as a side effect of
first use.

### 3.3 `enable` / `disable` operations (§2.3)

`enable` (idempotent; re-run = upgrade):

1. Deposit `<repo>/.punt-labs/vox/CLAUDE.md` (the surface-aware guide, §3.4),
   overwriting the vendored zone wholesale.
2. Write `<repo>/.punt-labs/vox/enabled`.
3. Add `@.punt-labs/vox/CLAUDE.md` to `<repo>/CLAUDE.md` if absent (never twice).
4. Additively register repo-scoped hook/permission entries in
   `<repo>/.claude/settings.json` (§2.8), if any.

`disable`:

1. Remove the import line from `<repo>/CLAUDE.md`.
2. Delete the `enabled` marker.
3. Deregister the settings entries it added.
4. Leave the rest of `.punt-labs/vox/` dormant (non-destructive, §2.9).
   `disable --purge` removes the subtree.

### 3.4 The surface-aware deposited guide — the intersection deliverable

`.punt-labs/vox/CLAUDE.md` is rewritten so a single static file drives vox from
whichever surface the agent has:

- A short preamble: "If the `mic` MCP tools are available, use them (below);
  otherwise drive vox through the `vox` CLI (below)."
- The MCP/slash section (existing content: `mic:unmute`, `mic:speak`, `/vox`,
  `/unmute`, …) — for plugin agents.
- A **CLI section** documenting the agent-relevant `vox` commands (speak/say,
  `vox notify`, `vox status`, `vox voices`, …) — so a `--no-plugin` agent knows
  how to invoke vox. Respect the settled metaphor (DES-042: MCP `unmute` is the
  agent flipping its own mic on; CLI is a discrete invocation) — the CLI section
  states when an agent appropriately shells out to `vox`.

The same file is deposited at repo scope by `enable` and at user scope by
`install` (§2.5).

The model surfaces a content constraint here: the **CLI section is
unconditionally present** in the file — the plugin-less agent is the *general*
case (`CliAgentGuided` read at `pluginPresent = false`), not a special branch.
The guide never gates its CLI content on plugin absence; the preamble routes the
agent to the surface it has, but both sections always ship.

The Z model (`docs/vox-enable-disable.tex`, `fuzz -t` clean, ProB-verified) is
the authoritative statement of the state machine; §4 mirrors it. It caught three
gaps in an earlier §4 draft — the `Purge` orphan-import, the `Disable` framing,
and the boolean-vs-line-count — all folded in above.

### 3.5 Hook gating on the marker

Vox's plugin session hooks (Stop/PostToolUse chimes, narration) currently fire
in every project the plugin is installed in. Under enablement they **gate on the
marker** (§2.7):

```sh
[ -f "$REPO_ROOT/.punt-labs/vox/enabled" ] || exit 0
command -v vox >/dev/null 2>&1 || exit 0
```

So vox only chimes/narrates in repos that opted in, and a clone of a
marker-enabled repo on a box without vox is a graceful no-op. This is what makes
per-repo enablement meaningful rather than cosmetic.

### 3.6 The CLAUDE.md import writer (§2.4)

vox originated the atomic/byte-preserving import writer (`src/punt_vox/claude_md.py`,
`GlobalClaudeImports`). Port it from user-scope to repo-scope and drop its
retired managed-section markers. The full §2.4 contract must hold: exact
canonical line `@.punt-labs/vox/CLAUDE.md`; append-if-absent, terminator-
insensitive match; skip fenced/indented code blocks (balanced-pair fence
semantics, unterminated-opener guard); the shared sibling lock
`.<host>.punt-import.lock`; atomic rename; byte-preserving host EOL;
symlink-resolving; mode-preserving. The `.claude/settings.json` writes (§2.8)
take the same lock (`.settings.json.punt-import.lock`).

### 3.7 `install.sh --no-plugin` (install-cli-only.md)

- Parse `--no-plugin` from `"$@"`; honor `VOX_NO_PLUGIN=1` identically; unknown
  flags exit 2 with usage.
- When set, skip only the marketplace + plugin steps (7–8). Binary install,
  PATH, dirs, seed, and the user-scope `install` enable/import still run.
- Both `curl … | sh -s -- --no-plugin` and `curl … | VOX_NO_PLUGIN=1 sh` work.
- README documents the default and the `--no-plugin` one-liner.

## 4. State machine (input to the Z model)

Model the per-repo enablement as a small state machine; `fuzz`-clean, and
ProB-checked for the biconditional invariant.

```text
State  RepoEnablement
  dirPresent    : BOOL     -- .punt-labs/vox/ exists
  markerPresent : BOOL     -- .punt-labs/vox/enabled exists
  importPresent : BOOL     -- exactly-one canonical @-import in CLAUDE.md
  invariant: markerPresent  <=>  importPresent          -- §2.11 biconditional
             markerPresent  =>   dirPresent             -- marker lives in the dir
```

Observable states: `Enabled` (all present), `Dormant` (dir present, marker &
import absent), `Absent` (nothing).

Operations (each preserves the invariant):

- `Enable`  : any state → `Enabled`. Post: dirPresent ∧ markerPresent ∧
  importPresent; idempotent (Enable∘Enable = Enable); import appears exactly
  once (no duplicate).
- `Disable` : any state → `Dormant` (if dir present) or `Absent`. Post:
  ¬markerPresent ∧ ¬importPresent ∧ **`dirPresent' = dirPresent`** — a *frame*,
  not an assertion of presence. Disable neither creates nor removes the dir;
  ensuring a dir would conjure a spurious `Dormant` state (empty
  `.punt-labs/vox/`, no marker) when run on an already-`Absent` repo.
- `Purge`   : any state → `Absent`. Post: ¬markerPresent ∧ **¬importPresent** ∧
  ¬dirPresent — i.e. `purge` is `disable` (**remove the import line**) *then*
  remove the subtree. Removing only the dir would, from `Enabled`, leave
  `(dir F, marker F, import T)` — violating the biconditional and leaving a
  404ing orphan `@`-import (§2.11 hard fail).

**The import is a line-count, not a boolean, for §2.4.** A boolean treats one
import and two identically, hiding "`disable` removes *every* match" and "a
second `enable` adds none." The Z model's `ImportFile` machine (`importLines :
0..2`) makes them checkable: `AppendImport` is 0→1→1 (never grows a duplicate on
its own); only `RemoveImport` (i.e. `disable`) repairs a pre-existing duplicate
2→0. The implementation must ensure `disable` heals a racing writer's duplicate.

Predicates (not transitions), modeled for their invariants:

- `HookFires`   ⟺ markerPresent ∧ toolInstalled.
- `AuditPass`   ⟺ (markerPresent ⟺ importPresent) ∧ (importPresent ⇒ guideFilePresent)
  ∧ (at most one import line).
- `CliAgentGuided(--no-plugin)` : after `Enable` on a plugin-less box,
  importPresent ∧ the deposited guide contains the CLI-drive section — the
  intersection property.

The import-write operation itself is modeled for the §2.4 idempotence: appending
when absent adds exactly one line; a second `Enable` adds none; `Disable`
removes every match (collapsing an accidental duplicate to zero).

## 5. Write-set (one PR)

- `install.sh` — `--no-plugin` / `VOX_NO_PLUGIN` parsing, skip plugin steps.
- `src/punt_vox/claude_md.py` — repo-scope import writer (ported; retired
  markers removed).
- New: `vox enable` / `disable` CLI commands (`__main__.py` + a commands module),
  the marker + settings.json registration + guide deposit.
- `src/punt_vox/server.py` — a `mic` MCP tool taking `action: enable|disable`.
- `commands/enable.md`, `commands/disable.md` — slash commands; retire the
  `y|n|c` arm of `commands/vox.md`, add `vox notify normal|continuous`.
- `hooks/*.sh` — marker gate.
- `.punt-labs/vox/CLAUDE.md` guide content — rewritten surface-aware (the
  intersection deliverable). (This is the file `enable`/`install` deposit; its
  source lives in the repo.)
- `README.md` — `--no-plugin` one-liner + the enable/disable usage.
- Tests: enable/disable idempotence + the biconditional, the import-writer §2.4
  contract, the hook gate, the `--no-plugin` install path, and the
  no-plugin+enable CLI-agent guidance scenario.

## 6. Acceptance

- `vox enable` in a repo → marker + import + guide + (if any) settings; re-run is
  a clean no-op/upgrade; `vox disable` reverses import + marker, leaves the dir.
- `/vox enable` and `vox enable` produce byte-identical markers.
- Plugin session hooks fire only in enabled repos.
- `install.sh --no-plugin` installs the CLI, no plugin; `vox enable` on that box
  deposits a guide whose CLI section lets an agent drive vox — verified by
  reading the composed `CLAUDE.md` and issuing a `vox` CLI call.
- `punt audit` clean (enabled ⟺ import; no orphan import; no legacy markers).

## 7. Rejected alternatives

- **Keep `/vox y|n` as a per-user layer.** Rejected: vox's notify is per-repo,
  so it is enablement, and §2.3 retires `y|n` there.
- **Two PRs.** Considered (independent rollback units) but rejected per the
  operator's one-PR preference and because `--no-plugin` only makes sense once
  `vox enable` is the CLI-only enablement path; the PR sequences enable/disable
  first, then `--no-plugin`.
- **A separate session on/off.** Not needed: enablement is the repo marker;
  per-invocation silencing stays on `/mute`/`/unmute`.
