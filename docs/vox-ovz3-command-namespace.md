# vox-ovz3: `/model` and `/provider` Collide With Claude Code Built-Ins

**Bead:** vox-ovz3
**Status:** design, ratified by operator with one amendment (2026-08-25) —
also namespace `/recap` (`/vox:recap`), as a preemptive consistency call
with no known collision. See §3.
**Author:** mdm (design mission)

## 1. The confirmed root-cause mechanism

Investigation question 1 asked which of four mechanisms controls bare
(`/model`) vs. namespaced (`/vox:model`) invocability. It is **(a): purely a
function of where the command file lives.**

Claude Code's own rule, stated in the org standard
(`punt-kit/standards/plugins.md`, "SessionStart hook" and "Command
Deployment" sections):

> Marketplace plugins provide namespaced commands automatically (`biff:who`),
> but top-level commands (`/who`) must be deployed to `~/.claude/commands/`
> by the SessionStart hook.

In other words: **every plugin command is namespaced-only by default.** A
bare top-level form only exists because a `SessionStart` hook copied the
command's `.md` file out of the plugin and into the user's global
`~/.claude/commands/` directory — an *opt-in*, per-command mechanism, not an
automatic consequence of shipping a command in a plugin.

`plugin/hooks/session-start.sh`'s "Deploy top-level commands" block (lines
88–125) does this copy **unconditionally** for every `plugin/commands/*.md`
file except `*-dev.md`:

```bash
for cmd_file in "$PLUGIN_ROOT/commands/"*.md; do
  name="$(basename "$cmd_file")"
  [[ "$name" == *-dev.md ]] && continue
  dest="$COMMANDS_DIR/$name"
  ...
  cp "$cmd_file" "$dest"
done
```

`model.md` and `provider.md` (and `voice.md`) have no exemption from this
loop, so they land in `~/.claude/commands/model.md` and
`~/.claude/commands/provider.md` — indistinguishable, to Claude Code, from a
command the user hand-wrote. That registration is what creates the bare
`/model` and `/provider` forms. It has nothing to do with the plugin's
native namespace (`/vox:model`, which the plugin provides automatically and
unconditionally, per the org standard above) and nothing to do with the
`Skill()` permission grants — those only pre-approve an invocation that
already exists; they do not create one. Investigation question 1(c) is
answered: `Skill(model)` does not expose the bare form. The deployment copy
does.

This is a **design mismatch, not a Claude Code bug.** `model.md`,
`provider.md`, and `voice.md` each carry an H1 header of `# /vox:<name>
command`, and this repo's own `CLAUDE.md` / `.punt-labs/vox/CLAUDE.md` both
document the three as `/vox:model [<name>]`, `/vox:provider [<name>]`,
`/vox:voice [<name>]` — namespaced-only, on purpose. The `session-start.sh`
deploy loop was never told that; it treats all nine command files the same.

### How the three commands ended up unexempted

`git log --oneline --diff-filter=A -- '**/model.md'` shows `model.md`,
`provider.md`, and `voice.md` were introduced as their own command files in
`e6a49f6` ("feat(vox-0rp9 Unit A): model, provider, voice — one command per
switch on every surface (#399)"). Before that, `model`/`provider` were
`$ARGUMENTS` subcommands dispatched inside `vox.md` — see
`DES-060` in `DESIGN.md`, which explicitly chose that shape for `enable`/
`disable` specifically **to avoid** claiming a bare global slash verb:

> Claude Code installs a plugin's commands into a single global namespace,
> so those two bare verbs claimed `/enable` and `/disable` for every
> session — generic names no plugin should own.

vox-0rp9 (#399) reversed that shape for `model`/`provider`/`voice`, splitting
them into their own files and switching to the plugin's native
`/vox:<name>` namespacing — but the `session-start.sh` deploy loop, written
for the DES-060-era "everything is `/vox <subcommand>`" world, was never
updated to exclude the three files it should no longer be touching. The
`Skill(model)`, `Skill(provider)`, `Skill(voice)` grants were added in the
same PR, alongside the deploy gap — a straight copy of the existing
`Skill(unmute)`/`Skill(mute)`/… pattern with no reconsideration of whether
these three specifically should be bare-grantable at all.

## 2. Investigation question 2: does `/provider` collide today?

No known Claude Code built-in currently named `/provider`. It is not an
active collision. But it is the same defect by the same mechanism, and it
already contradicts its own command file's header and this repo's
documentation (`CLAUDE.md` lists `/vox:provider`, never bare `/provider`).
Fix it in the same change as `/model` — see §3.

## 3. Scope: `model`/`provider`/`voice`/`recap`, not all nine commands

**Amended by operator ruling (2026-08-25): scope the fix to `model.md`,
`provider.md`, `voice.md`, and `recap.md`. Leave `unmute.md`, `mute.md`,
`vibe.md`, `music.md`, `vox.md` bare-deployed exactly as they are.**

This is not "only fix the two known collisions and hope." It is a
consistency read of what the codebase already says these commands are, plus
one operator-directed exception:

| Command | H1 header | Docs (`CLAUDE.md`) | Intended invocation |
|---|---|---|---|
| `unmute.md` | `# /unmute command` | `/unmute` | bare |
| `mute.md` | `# /mute command` | `/mute` | bare |
| `vibe.md` | `# /vibe command` | `/vibe <mood>\|auto\|off` | bare |
| `music.md` | `# /music command` | `/music on\|stop\|...` | bare |
| `vox.md` | `# /vox command` | `/vox enable\|disable` | bare |
| `model.md` | `# /vox:model command` | `/vox:model [<name>]` | **namespaced** |
| `provider.md` | `# /vox:provider command` | `/vox:provider [<name>]` | **namespaced** |
| `voice.md` | `# /vox:voice command` | `/vox:voice [<name>]` | **namespaced** |
| `recap.md` | `# /recap command` (today) | `/recap` (today) | **namespaced** *(operator amendment — no known collision)* |

Five commands are deliberately bare — short, mnemonic, session-scoped verbs a
user reaches for constantly (`/mute`, `/unmute`, `/vibe`, `/music`, `/vox`).
`vox.md`'s own body text says as much: "The three mid-session switches —
model, provider, and voice — each live on their own top-level slash command
now (`/vox:model`, `/vox:provider`, `/vox:voice`)," explicitly contrasting
them with `/vox` itself. The three switch commands are deliberately
namespaced — they are lower-frequency, config-mutating, and (per this bead)
exactly the class of name a host application is most likely to also want
(`model` is *already* taken; a future Claude Code release could equally
introduce a built-in `/voice` or `/provider`).

**`/recap` is the one exception, and it is not a collision fix.** No
Claude Code built-in named `/recap` is known today; nothing in this
investigation found or expects one. The operator ruled it into the
namespaced group anyway, as a **preemptive consistency call**: `/recap` is
the same shape of command as `/model`/`/provider`/`/voice` — a discrete,
config-adjacent utility verb, not a high-frequency toggle like `/mute` — and
the operator's judgment is that the whole class should be namespaced now,
rather than waiting for a future Claude Code release to collide with it the
way `/model` did. Read this bead's history straight: `/recap` moves into
`/vox:recap` **because the operator chose it, not because a bug was found.**
A future reader of this design doc, or of `session-start.sh`'s
`NAMESPACED_ONLY` list, should not go looking for a `/recap` collision
report — there isn't one.

Forcing all nine into namespaced-only would still be a real product
regression: `/mute`, `/unmute`, `/vibe`, `/music`, and `/vox` are used
constantly and the bare form is the documented, intended UX — collapsing
`/unmute` to `/vox:unmute` fixes nothing (there is no collision) and breaks
muscle memory for zero benefit. The correct fix is to make the deployment
mechanism honor the distinction the command files declare — now including
the operator's one deliberate reclassification of `/recap` — not to erase
the distinction everywhere.

**Future-proofing beyond this bead:** if a future Claude Code release adds a
new built-in that collides with one of the five remaining intentionally-bare
commands, that is a new bug to fix then (rename or re-scope that one
command), not a reason to preemptively namespace everything now. The org
standard (`plugins.md`) treats bare top-level commands as a normal,
supported pattern — removing them everywhere "just in case" would be
over-engineering against a hypothetical, at the cost of the working UX
today. `/recap` is the one place the operator chose to make that trade
early; it is not a precedent this design applies to the other four on its
own authority.

## 4. The fix

### 4.0 Addressing the "deletion-only" hypothesis

Before finalizing the write-set, the leader raised a narrower hypothesis
worth testing on its own merits: since `/vox:model` and `/vox:provider`
**already exist and already work today** as correctly-namespaced commands,
maybe the whole fix is a deletion — drop the bare `Skill(model)` /
`Skill(provider)` grants from `session-start.sh` and nothing else, on the
theory that the grant itself is what exposes the bare form.

**Verdict: no — that alone does not fix the bug.** Evidence:

- `punt-kit/standards/plugins.md` — the org's own standard, whose "Version
  field precedence" section cites Claude Code's actual source
  (`src/utils/plugins/pluginVersioning.ts`), i.e. it was written from
  reading Claude Code's implementation, not guessed — is explicit that
  **two separate mechanisms** are in play: "Deploy top-level commands" (the
  `SessionStart` hook copying files into `~/.claude/commands/`, which is
  what makes a bare form exist at all) and "Auto-allow MCP tool
  permissions" (the `Skill()`/tool-glob grants, which only avoid a
  permission prompt for an invocation that already exists). Nothing in that
  document, nor anywhere else read during this investigation, describes
  `Skill()` as gating command *registration* or *visibility* — only
  *execution without a prompt*.
- I attempted to verify this independently by extracting and grepping the
  strings table of the installed Claude Code client binary
  (`~/.local/share/claude/versions/2.1.234`, a bundled/minified JS binary —
  no readable source available locally to inspect directly). I found
  confirming context for the same separation — e.g. a log-message string
  `"getSkills returning: skill dir commands, plugin skills, bundled
  skills, builtin plugin skills"` and a distinct `"built-in slash commands
  stay typable but are hidden from the model"` string tied to a different,
  unrelated flag (`CLAUDE_CODE_DISABLE_BUNDLED_SKILLS`) — but the binary is
  minified enough (identifiers are single/double-letter, JS is packed onto
  megabyte-scale single lines) that I could not extract the actual
  precedence/registration function body to *positively* confirm the
  Skill()-alone-is-insufficient claim from source, only to fail to find
  anything contradicting it. This is a documentary-evidence-plus-absence-
  of-counterevidence conclusion, not a source-code proof.
- Mechanically: `~/.claude/commands/model.md` is documented elsewhere in
  the same standard as a **personal custom slash command** — the same
  mechanism a user gets by hand-creating a file in that directory with no
  plugin involved at all. Personal custom commands are not part of the
  permission-grant system; they are registered by file presence. Deleting
  a `Skill()` entry does not delete, rename, or hide that file.

**So: removing `Skill(model)`/`Skill(provider)` without also removing the
files from `~/.claude/commands/` would, at best, cause `/model` and
`/provider` to prompt for permission every time before running vox's
command — a friction, not a fix. The ambiguous/wrong resolution the bead
reports (vox's command answering a name a user expects to mean "switch
Claude's own model") persists either way, because the *file* is what
Claude Code consults to decide `/model` means "run this command," and nothing
proposed in the deletion-only hypothesis touches that file.

**What IS a real, smaller-than-expected fix, if the empirical test in §5
confirms it:** if `Skill(vox:model)` alone (no bare `Skill(model)` entry)
turns out to be sufficient for `/vox:model` to run without a prompt, then
§4.3's "which grant name" sub-question resolves for free, and the
write-set really is almost pure deletion — remove the deploy-loop entries
for the three files (§4.1, unavoidable regardless) and simply drop the
bare `Skill()` grants rather than replace them with anything. That is
already what §4.1 and §4.3 below specify; the leader's hypothesis is folded
in as the first thing to test, not a competing design.

### 4.1 `plugin/hooks/session-start.sh`

**Exclude `model.md`, `provider.md`, `voice.md`, `recap.md` from the
top-level deploy loop**, the same way `*-dev.md` is already excluded:

```bash
NAMESPACED_ONLY=(model.md provider.md voice.md recap.md)
for cmd_file in "$PLUGIN_ROOT/commands/"*.md; do
  name="$(basename "$cmd_file")"
  [[ "$name" == *-dev.md ]] && continue
  for skip in "${NAMESPACED_ONLY[@]}"; do
    [[ "$name" == "$skip" ]] && continue 2
  done
  ...
done
```

**Retire the four files from any install that already deployed them.** Per
DES-060 precedent (`enable.md`/`disable.md` were added to the `RETIRED`
array so an already-installed plugin drops the stale top-level command on
its next session start) and per the org's "no migration/compat code" rule —
this is *not* a migration bridge, it is deleting a superseded artifact the
same way every other retired command already is:

```bash
RETIRED=(say.md speak.md notify.md vox-on.md vox-off.md enable.md disable.md \
  model.md provider.md voice.md recap.md)
```

Without this, every session that already ran the old `session-start.sh`
once keeps its stale `~/.claude/commands/model.md` (and, once this design
ships, `recap.md`) forever — the plugin update alone does not un-deploy a
file it previously deployed. This is the step that actually fixes the bug
for existing users; the deploy-loop exclusion alone only prevents *new*
installs from acquiring the collision (and, for `recap.md`, only prevents
new installs from keeping the bare form the operator wants retired).

**`Skill()` grants — open question, resolve empirically (§4.3). Applies to
all four names, including `recap`, not just the two that collide.**

### 4.2 Command files: `model.md`/`provider.md`/`voice.md` need no change; `recap.md` does

`model.md`, `provider.md`, `voice.md` already carry the correct
`/vox:<name>` H1 headers and usage sections. Nothing in their content needs
to change — the defect for those three is entirely in the deployment
mechanism, not in what the commands claim to be.

`recap.md` is different: today it carries `# /recap command` as its H1 and
documents `/recap` (bare) in its own "Usage" section, and both
`punt-labs/vox/CLAUDE.md` and `.punt-labs/vox/CLAUDE.md` list it under
"Slash commands" as bare `/recap`. Unlike model/provider/voice, whose docs
already matched the namespaced-only intent and only the deploy mechanism
was wrong, `recap.md`'s own content actively documents the bare form as
correct. The implementation mission must, in the same change:

A repo-wide grep for `/recap` (run during this design mission, so this list
is concrete, not a placeholder for the implementation mission to
rediscover) turns up every place the bare form is asserted as correct
usage — as opposed to the many CHANGELOG/DESIGN entries that are historical
narration of past PRs and must NOT be rewritten (rewriting history in
`CHANGELOG.md`/`DESIGN.md` misrepresents what shipped at the time):

- `plugin/commands/recap.md:6` — H1: `# /recap command` → `# /vox:recap command`.
- `plugin/commands/recap.md:12` — Usage section: `` `/recap` `` → `` `/vox:recap` ``.
- `src/punt_vox/assets/global-guidance.md:133` — the deployed
  `~/.punt-labs/vox/CLAUDE.md` guide's own "Slash commands" list, currently
  `` - `/recap` — speak a 2–3 point summary of your last response. `` This is
  a **source asset that ships to every consuming repo** via the
  self-registering `@`-import (see `CHANGELOG.md`'s vox-ys4z entry) — it is
  the single highest-priority line to fix, since every project using vox
  reads this exact file at session start.
- `.punt-labs/vox/CLAUDE.md:133` — same line, this repo's own local copy of
  the deployed guide (dogfooding the asset above).
- `CLAUDE.md:90` (repo root) — the `plugin/commands/` table row listing
  `` `/vox`, `/unmute`, `/mute`, `/recap`, `/vibe`, `/music`, `/model`,
  `/provider`, `/voice` `` → move `/recap` next to `/model`/`/provider`/
  `/voice` or otherwise mark it namespaced consistently with them.
- `README.md:30` — "Drive it with `/vox enable`, `/recap`, `/vibe`, `/music`."
- `README.md:44` — a usage-example code block: `` /recap        # spoken
  summary of what just happened ``.
- `README.md:198` — an example invocation: `` /recap ``.
- `README.md:314` — the commands reference table row:
  `` | `/recap` | Spoken summary of Claude's last response | ``.

`README.md:21-23` ("Task recap", "Same recap") are unrelated — English
prose describing audio sample clips, not the slash command — leave those
alone. `DESIGN.md` (DES-005, the `/recap` ADR; the vox-0qi stale-command
cleanup entry) and `CHANGELOG.md` (vox-ys4z, vox-ewh, vox-ehf entries, and
the original `/recap` shipping entry) are historical record of what those
PRs actually did at the time and are **not** rewritten — this is the same
"no rewriting history" discipline already implicit in how this repo treats
its own changelog.

### 4.3 Open question: does `Skill(model)` need to become `Skill(vox:model)`?

Applies identically to `recap`: whatever grant form resolves for
`model`/`provider`/`voice` resolves for `recap` too — it is the same
mechanism on a fourth namespaced command, not a separate sub-question.

Investigation question 1(c) asked whether the `Skill()` grant name itself
needs to track the namespace. This is **genuinely unresolved from static
reading** and the mission brief is right to flag it rather than let it be
guessed. Two facts pull in different directions:

- `plugins.md`'s "Namespace scope" table lists a *production* skill ID with
  a colon: `punt:reconcile`. That is evidence a namespaced command's
  underlying skill name is `<plugin>:<command>`, not the bare command name
  — which would mean the current `Skill(model)` grant has *never* actually
  matched a `/vox:model` invocation, and every `/vox:model` call has been
  hitting a real permission prompt that the bare `/model` deployment
  happened to mask (because the bare copy's `Skill(model)` grant matched
  fine).
- But that example is for a plugin (`punt`) that dispatches its subcommands
  through `$ARGUMENTS` inside one command file (`/punt init` per the same
  table's Commands row, no colon) — a different shape from vox's
  one-command-file-per-operation `model.md`/`provider.md`/`voice.md`, so it
  is not a confirmed analog.

**Recommendation for the implementation mission:** change the grant to
`Skill(vox:model)`, `Skill(vox:provider)`, `Skill(vox:voice)`,
`Skill(vox:recap)` (dropping the bare `Skill(model)`/`Skill(provider)`/
`Skill(voice)`/`Skill(recap)` entries), then verify empirically per §5
whether `/vox:model` still triggers a permission prompt.
If it does, the correct grant name is something else (candidates to try in
order: the bare form again, `Skill(vox_model)`, or whatever name the
permission prompt itself displays — Claude Code's prompt names the skill it
is asking about) — this is a five-minute dev-plugin check, not a design
fork worth blocking on. Do not ship a guess; ship whatever the dev-plugin
session actually proves clears the prompt.

### 4.4 `scripts/check-skill-permissions.sh`

The gate currently requires exact bare-name parity between
`plugin/commands/*.md` basenames and `Skill(<name>)` grants. If §4.3 lands
on a namespaced grant name (`Skill(vox:model)`), this script needs two
changes to stay meaningful rather than start firing false positives:

1. The `ALLOWED` extraction regex (`grep -oE 'Skill\([a-z_-]+\)'`) must
   accept a colon: `Skill\([a-z_:-]+\)`.
2. The matching loop must accept either the bare command name or
   `vox:<command name>` as satisfying a given `plugin/commands/<name>.md` —
   because `model`/`provider`/`voice`/`recap` will now be satisfied only by
   the namespaced form, while the other five commands are still satisfied
   only by the bare form. A concrete way to encode this without a special case
   per command: read the same `NAMESPACED_ONLY` list `session-start.sh`
   introduces (§4.1) — or a `scripts/lib` shared list, if one is worth
   extracting — and require the namespaced grant for names on that list,
   the bare grant for names not on it.

If §4.3 instead concludes the bare `Skill(model)` grant is correct after
all (i.e. the namespace does not affect the skill ID), skip this section
entirely — no change needed to the checker.

## 5. Verification plan (for the leader/implementation mission to execute)

This is a Claude Code UI/invocation behavior. No Python test exercises it;
`make check` passing is necessary but proves nothing about which slash name
resolves to what. Use dev-plugin testing per this repo's `CLAUDE.md` §
"Dev plugin testing":

```bash
uv tool install --force --editable .        # working tree = installed vox
claude --plugin-dir plugin                  # loads plugin as vox-dev
```

Because the dev/prod split (`PLUGIN_MODE`) only deploys top-level commands
in **prod** mode (`session-start.sh` line 86: "In dev mode, skip command
deployment — prod plugin handles top-level commands"), the collision itself
only reproduces against a **prod-mode** install — a plain marketplace
install, or a local checkout with `plugin.json`'s `name` temporarily read as
non-`-dev` (e.g. running the release-plugin swap script against a scratch
copy, or testing against an already-installed marketplace `vox`
side-by-side with `vox-dev`). Write down expected vs. actual before running,
per the org verification standard:

1. **Before the fix (reproduce).** In a session with the *current* prod
   plugin already installed (or after manually running the current
   `session-start.sh` against a prod-named manifest), confirm
   `~/.claude/commands/model.md` and `~/.claude/commands/provider.md`
   exist, and that typing `/model` in Claude Code offers vox's model-switch
   command rather than (or ambiguously alongside) Claude Code's built-in
   model picker. Expected: bug reproduces. `/recap` has no collision to
   reproduce — its check starts at step 2.
2. **After the fix, fresh install.** Simulate a session start with no prior
   `~/.claude/commands/model.md` (a clean `$HOME/.claude/commands`, or a
   throwaway `$HOME`). Run the updated `session-start.sh` (or restart a
   session pointed at the fixed plugin). Confirm `~/.claude/commands/`
   contains **no** `model.md`, `provider.md`, `voice.md`, or `recap.md`.
   Confirm typing `/model` shows only Claude Code's own built-in — vox does
   not appear. Confirm `/vox:model` (or `/vox-dev:model` in dev mode — the
   plugin namespace gets the `-dev` suffix, not the command name; there is
   no `model-dev.md`) **does** invoke vox's model switch, with no permission prompt if §4.3's
   fix is correct (or exactly one prompt, granted once, if it is not —
   either way, confirm which). Repeat the same "does invoke, no unexpected
   prompt" check for `/vox:recap` — there is no bare-form regression to
   check for `/recap` specifically (nothing built-in claims that name), so
   the assertion here is narrower: `/vox:recap` works, and `/recap` typed
   bare no longer offers vox's command at all (Claude Code either reports
   no matching command or falls through to whatever else, if anything,
   claims that name).
3. **After the fix, upgrade from a stale install.** Start from a `$HOME`
   that already has the *old* `~/.claude/commands/model.md` /
   `provider.md` / `voice.md` / `recap.md` present (simulating an existing
   user). Run the updated `session-start.sh`. Confirm the `RETIRED` cleanup
   removes all four, and the `hookSpecificOutput` message names them as
   cleaned (mirrors the existing `enable.md`/`disable.md` retirement
   message).
4. **Regression check on the five bare commands.** Confirm `/mute`,
   `/unmute`, `/vibe`, `/music`, and `/vox` still deploy to
   `~/.claude/commands/` and still invoke correctly bare, unaffected by the
   `NAMESPACED_ONLY` exclusion.
5. **`make lint`.** `scripts/check-skill-permissions.sh` passes with the
   new grant scheme (§4.4) — run it directly for a fast check before
   involving Claude Code at all: `./scripts/check-skill-permissions.sh`.

Every step above is something the leader confirms by *looking* (file
existence, typed-slash behavior, the hook's own JSON output) — there is no
introspection API for "which plugin claimed this slash name," so this is a
manual-but-precise playbook, not a vague "try it and see."

## 6. File-level write-set for the implementation mission

| File | Change |
|---|---|
| `plugin/hooks/session-start.sh` | Add `NAMESPACED_ONLY=(model.md provider.md voice.md recap.md)` and skip them in the top-level deploy loop (§4.1); add `model.md provider.md voice.md recap.md` to `RETIRED` (§4.1); change the four `Skill()` grants per §4.3's empirical result |
| `plugin/commands/recap.md` | H1 `# /recap command` → `# /vox:recap command`; Usage section `/recap` → `/vox:recap` (§4.2) |
| `src/punt_vox/assets/global-guidance.md` | Line 133 slash-commands list: `/recap` → `/vox:recap` (§4.2) — highest-priority doc fix, ships to every consuming repo |
| `.punt-labs/vox/CLAUDE.md` | Line 133, same fix, this repo's own deployed-guide copy (§4.2) |
| `CLAUDE.md` (repo root) | Line 90, `plugin/commands/` table row — reflect `/recap` as namespaced alongside `/model`/`/provider`/`/voice` (§4.2) |
| `README.md` | Lines 30, 44, 198, 314 — every usage/example reference to bare `/recap` → `/vox:recap` (§4.2); leave lines 21-23 alone, unrelated prose |
| `scripts/check-skill-permissions.sh` | Only if §4.3 lands on namespaced grants — widen the regex and matching logic per §4.4 |
| `CHANGELOG.md` | `## [Unreleased]` entry under `Fixed`, in the PR branch before merge, per this repo's documentation discipline — a new entry, not an edit to any historical entry |
| `docs/testing/manual-tests.md` | Optional: add the `/model` / `/vox:model` (and `/recap` / `/vox:recap`) non-collision check to the canonical manual flight, so this class of regression is covered on every future release, not just this bead — recommended but not required for this bead to close |

No changes to `model.md`, `provider.md`, `voice.md`, `unmute.md`, `mute.md`,
`vibe.md`, `music.md`, or `vox.md` — their content and headers are already
correct. `recap.md` is the one command file that DOES change content, per
§4.2 — it moves out of the "no changes" set with this amendment.
`DESIGN.md` and `CHANGELOG.md`'s *existing* entries are untouched (§4.2);
only a new `## [Unreleased]` `CHANGELOG.md` entry is added.

## 7. Constraints checked

- **No migration/compat/shim code.** The `RETIRED` addition is not a
  compat bridge — it is the existing forward-integration cleanup pattern
  (DES-060 precedent) that deletes a superseded artifact outright. Nothing
  detects an "old" vs. "new" format and branches; it unconditionally
  removes four specific filenames the same way the seven already-retired
  names are removed.
- **`punt-kit/standards/cli.md`** has no namespacing guidance beyond the
  PyPI/CLI naming table (unrelated to slash-command namespacing); the
  authoritative reference for this bug is `punt-kit/standards/plugins.md`
  §"Command Deployment" and §"SessionStart hook", cited throughout above.
- **`scripts/check-skill-permissions.sh`** stays green under this design —
  §4.4 describes the exact update it needs if the grant scheme changes; if
  it does not, no update is needed and the gate is unaffected.
