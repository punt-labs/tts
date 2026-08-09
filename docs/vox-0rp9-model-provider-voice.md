# Model, Provider, Voice — one command per concern on every surface

**Beads:** vox-0rp9.1 (model), vox-0rp9.2 (provider), vox-0rp9.3 (voice),
vox-0rp9.4 (strip `mic:unmute` overloads). Parent epic: vox-0rp9.

**Sequence:** implementation missions dispatch AFTER PR #396 (vox-0rp9.5,
`mic:notify` off-mode retirement) merges to main. This design is one document
covering four sibling units of one architectural change; the implementation
lands as at most two rollback-coherent PRs, per §6.

**Scope:** surface + schema change. No z-spec required — session state
(the `_voice`, `_provider`, `_model` fields on
[`SessionConfig`](../src/punt_vox/server.py) at server.py:107–226) is already
modelled by the property/setter contract; this change only re-routes writes
through a dedicated tool per concern.

**Forward-integrated by contract.** Every retired form is deleted in the same
commit as its replacement, per PL-PP-1 and the vox `CLAUDE.md` migration
hard-gate. The design carries no shim, no alias, no deprecation window, no
`# removed` tombstone. A leader review that finds one strikes it before
implementation dispatches.

---

## 1. Problem

Three surfaces (Claude Code slash, the `vox` CLI, the `mic` MCP tool) all
own the same three switches — TTS **model**, TTS **provider**, session
**voice** — and none of them agrees with the others on the verb, the
no-argument behaviour, or the tool that owns the write. The `mic:unmute`
MCP tool is where three of those writes secretly land, because it accepts
`model=`, `provider=`, and `voice=` as siblings to `text=` and treats a
call with only the switch arguments as a config write. That overload made
sense when there was no dedicated switch tool. It is misleading now: a tool
named "unmute" whose stated job is "synthesize and play audio"
(server.py:391) should not silently change global session state when
called without text.

### 1.1 The three-way surface inconsistency, verb by verb

| Concern | Slash today | CLI today | MCP today |
|---|---|---|---|
| **Model** | `/vox model <name>` (commands/vox.md:15, 24–34) — shorthand-resolves `v3`/`flash`/`turbo`/`multilingual`, calls `mic:unmute` with `model=` only | no verb — the `--model` flag rides on `vox say` (`ModelOpt` at `__main__.py:132–139`) | overloaded onto `mic:unmute` `model=` parameter (server.py:389, 438–439) |
| **Provider** | `/vox provider <name>` (commands/vox.md:16, 36–40) — calls `mic:unmute` with `provider=` and empty `model=` | no verb — the `--provider` flag rides on `vox say` and `vox voices` (`ProviderOpt` at `__main__.py:121–131`) | overloaded onto `mic:unmute` `provider=` parameter (server.py:388, 438–439) |
| **Voice** | `/unmute` no-arg lists via `mic:who`; `/unmute matilda` sets via `mic:speak` with `voice=` (commands/unmute.md:20–24) | `vox voice <name>` — errors when no arg (`__main__.py:472–482`); `vox voices` — separate list command (`__main__.py:489–519`) | `mic:who` — list only (server.py:562–600); write is overloaded onto `mic:unmute` `voice=` (server.py:377, 641–643), `mic:speak` `voice=` (server.py:672–675), and `mic:notify` `voice=` (server.py:641–643) |

Concrete misalignments a reader can observe from that table:

- The **verb naming** is not shared: slash has `/vox model` and `/unmute`;
  CLI has `vox voice` but nothing named `model` or `provider`; MCP has
  `mic:who` but nothing named `model`, `provider`, or `voice`.
- The **no-argument contract** is not shared: `/unmute` (no arg) opens a
  picker; `vox voice` (no arg) errors; `vox voices` (no arg) lists;
  `mic:who` always lists; `/vox` (no arg) prints a usage string
  (commands/vox.md:64–66); `mic:unmute` (no arg) either updates config
  or returns `Provide text or segments.` depending on whether any of the
  overloaded fields is set (server.py:443–449).
- The **write path** is not shared: some writes go through `mic:unmute`
  overloads, some through `mic:speak`, some through `mic:notify`, some
  through the config file directly (CLI `vox voice` writes
  `ConfigStore(...).write_field("voice", voice)` at `__main__.py:480`).

### 1.2 The `mic:unmute` overload

`mic:unmute` today performs four unrelated jobs, all through one schema:

1. **Say text.** Its stated purpose — synthesize `text` (or `segments`)
   and play (server.py:391–427).
2. **Switch model.** A call with `model=` and no text writes the model
   to the session and returns `{"status": "config updated", "model": ...}`
   (server.py:438–448).
3. **Switch provider.** Same shape, `provider=` (server.py:438–448).
4. **Switch session voice.** A call with `voice=` and no text falls
   through the same overload path — the `voice=` parameter is not on
   the `given`-updates dictionary (server.py:445), so today a `voice=`-only
   call actually errors with `Provide text or segments.`. But this is
   accidental: `voice=` **is** accepted, is documented as a top-level
   parameter (server.py:377, 396–399), and callers (including the current
   `/unmute` slash command via `mic:speak`) treat any surface that carries
   `voice=` as a session-voice switch.

The overload is why `commands/vox.md`, `commands/unmute.md`, and the
deposited guide (`assets/global-guidance.md`:34–44) each name a different
"canonical" way to change the three settings — every author picked a
different door, all of them working.

### 1.3 Why one design covers four beads

The four beads change the same surface files (`server.py`, `commands/*.md`,
`src/punt_vox/__main__.py`, `assets/global-guidance.md`, tests) and the same
architectural invariant ("one MCP tool per engine capability", cli.md
§Projection Strategy). Splitting the design across four docs would
duplicate the write-set and the retirement inventory four times. The
implementation is dispatched as separate missions so each has an airtight
write-set, but the contract they satisfy is one contract.

---

## 2. End-state per surface

The verb, the no-argument behaviour, and the write path are the same on
every surface. The differences are in output form (a picker in Claude
Code, plain text on a TTY, JSON to a machine consumer) — never in
semantics.

### 2.1 The end-state table (authoritative)

| Concern | Slash | CLI | MCP |
|---|---|---|---|
| **Model** | `/vox:model [<name>]` | `vox model [<name>]` | `mic:model(name?: str)` |
| **Provider** | `/vox:provider [<name>]` | `vox provider [<name>]` | `mic:provider(name?: str)` |
| **Voice** | `/vox:voice [<name>]` | `vox voice [<name>]` | `mic:voice(name?: str)` |
| **Enable/disable** (unchanged) | `/vox enable`, `/vox disable` | `vox enable`, `vox disable` | `mic:enablement(action)` |
| **Say text** (unchanged shape, stripped overloads) | (uses `mic:unmute`) | `vox say` | `mic:unmute(text\|segments)` |
| **Toggle spoken notifications** (unchanged) | `/unmute` (bare), `/mute` | `vox speak y\|n` | `mic:speak(mode)` |

Two structural choices this table encodes:

- **`/vox:<verb>` for the three non-enablement switches.** Enable/disable
  stays under `/vox` because tool-enable-disable.md §2.14 mandates
  `/<tool> enable` / `/<tool> disable` exactly, and DES-060
  (CHANGELOG.md at the 4.17.0 line for the /vox namespace consolidation)
  ratified that decision. Model, provider, and voice are unrelated to
  enablement — folding them under `/vox` was the mistake this epic corrects.
  Colon-namespacing (`/vox:model`) sits them alongside `/vox` in Claude
  Code's picker so the tool prefix is still discoverable, without stealing
  the top-level `/model`, `/provider`, `/voice` verbs that other tools
  might legitimately claim.
- **One MCP tool per concern.** `mic:model`, `mic:provider`, `mic:voice`
  each own one engine capability, matching how `mic:enablement`,
  `mic:speak`, `mic:vibe` are structured today. `mic:who` disappears —
  the list capability lives on `mic:voice` (no arg) because "the list"
  and "the write" are two shapes of one concern (which voice), not
  two concerns.

### 2.2 Verb contracts

Every switch tool takes one optional `name` argument. The `name`-absent
path always returns the list. The `name`-present path always writes and
returns a confirmation. There is no separate "set" verb, no `--list`
flag, and no `list_*` sibling tool — per cli.md §Subcommand naming ("single
verbs: `search`, `ingest`, `explain`, `talk`, `write`") and the pattern
`mic:enablement` already uses (server_enablement.py:45–80: one method,
one action argument, one return shape).

**Slash — `/vox:model` (also `/vox:provider`, `/vox:voice`).**

- No argument: call `mic:<concern>` with no arguments; unpack the reply's
  `available` list and `current` string; open `AskUserQuestion` with one
  question (see §4c for the dialog structure); on the user's pick, call
  `mic:<concern>` again with `name=<pick>`. No text output on either
  call — the audio panel and Claude Code's picker are the whole response.
- `<name>` (an argument the user typed): call `mic:<concern>` with
  `name="<name>"` directly. No text output.

**CLI — `vox model [<name>]` (also `vox provider`, `vox voice`).**

- No argument: print the roster, one per line, with the current selection
  suffixed `(current)`. Under `--json`, emit
  `{"names": [...], "current": "..."}` (the shape `mic:<concern>`
  returns, minus the tool-only `available` alias). Non-zero exit only on
  a daemon fault (`VoxdConnectionError`, `VoxdProtocolError`).
- `<name>`: write, then emit `{"<concern>": "<resolved-name>"}` under
  `--json`, or a one-line human confirmation. For `voice` the confirmation
  is the current "`<voice>'s here.`" line (`__main__.py:481`); for `model`
  and `provider` it is `Model: <resolved-name>` and
  `Provider: <resolved-name>`.
- The shorthand table for `model` (`v3` → `eleven_v3`, etc.) lives in
  Python — a new `resolve_model` function shared by the CLI and the MCP
  tool — not in the slash command's markdown as it does today
  (commands/vox.md:26–33).

**MCP — `mic:model(name: str | None = None) -> str` (also `mic:provider`,
`mic:voice`).**

- `name=None` (or omitted): return a JSON list
  `{"available": ["a", "b", ...], "current": "b"}`. For `mic:voice`, the
  reply carries the shape §3.3 specifies — the four fields `provider`,
  `current`, `available`, `featured` (the current `mic:who` payload with
  `all` renamed `available` for uniformity with the two new tools;
  `featured` is retained because the slash-command picker uses it, see
  §4c). For `mic:model` and `mic:provider`, `available` is the enum
  authored server-side (§3.1, §3.2).
- `name="<name>"`: resolve any shorthand (model only), write to the
  session (and to `.punt-labs/vox/vox.md` via `ConfigStore.write_field`,
  the same choke-point the CLI uses at `__main__.py:480`), and return
  `{"<concern>": "<resolved-name>"}`. For `mic:provider` (a closed enum,
  §3.2) the `Literal[...]` schema rejects an unknown name at the FastMCP
  boundary before the handler runs — the daemon-standard error envelope
  `{"error": "..."}` is not reachable on the input path. For `mic:model`,
  the runtime lookup `resolve_model(name, provider)` can still yield
  "provider has no user-selectable model" or "unknown shorthand for
  provider X"; that path returns `{"error": "..."}` per every other
  tool's contract (server.py:364–366).

### 2.3 Return-shape parity

Both surfaces must return the **same field set** for the same daemon
state, per the pattern
`tests/test_music_surface_parity.py::test_status_reports_the_same_field_set`
established in vox-bx7b. §5 spells out the tests; the shape they
enforce is:

- **List response** (no argument on any surface):
  `{"available": [...], "current": "..."}` for MCP;
  `{"names": [...], "current": "..."}` for CLI `--json`
  (rename `available`→`names` on the CLI to match the existing music
  parity precedent where the CLI carries a verb-shaped key and the
  MCP carries a domain-shaped key). The parity test compares the
  sequence values and the current selection; the key rename is a
  documented `_CLI_ONLY` axis, matching test_music_surface_parity.py:45.
- **Write response** (name argument): `{"<concern>": "<resolved-name>"}`
  on both surfaces. Same key, same value — no rename.

---

## 3. Enumerations

### 3.1 Models (per provider)

Models are provider-specific. `mic:model` (no arg) returns the models
available for the **currently selected provider**, plus the shorthand
aliases the CLI and slash accept. The enum lives in a new
`punt_vox.models` module (a single dataclass table); the current
scattered constants (elevenlabs.py:35, 39–43) are re-exported through it.

| Provider | Full names | Shorthand aliases |
|---|---|---|
| `elevenlabs` | `eleven_v3` (default), `eleven_flash_v2_5`, `eleven_turbo_v2_5`, `eleven_turbo_v2`, `eleven_multilingual_v2` | `v3`, `flash`, `turbo`, `multilingual` |
| `openai` | `tts-1`, `tts-1-hd`, `gpt-4o-mini-tts` | (none) |
| `polly` | (none — Polly has voice engines, not user-selectable models) | (none) |
| `say` | (none) | (none) |
| `espeak` | (none) | (none) |

For a provider with no models, `mic:model` (no arg) returns
`{"available": [], "current": null}` and `mic:model` with a `name`
argument returns
`{"error": "provider <p> has no user-selectable model"}`. The slash
command reads the same reply and shows a one-line
"No models for this provider" note in place of the picker.

Shorthand resolution is a pure function `resolve_model(name, provider) -> str`
in `punt_vox.models`; both the MCP tool and the CLI call it.

### 3.2 Providers

A closed enum: `elevenlabs`, `openai`, `polly`, `say`, `espeak` — the
five names already carried in the shorthand default-voice table
(`providers/__init__.py:39–43`). No provider is discovered at runtime;
adding one is a code change. The `mic:provider` schema uses
`Literal["elevenlabs", "openai", "polly", "say", "espeak"] | None` so
an unknown name is rejected before dispatch.

Provider list is **static** — unlike voice/model list it does not require
a reachable daemon, matching vox-0rp9.2 §Notes.

### 3.3 Voices

The voice roster is provider-specific and lives at the provider — the
current `mic:who` path (server.py:574–599) fetches it via
`VoxClientSync().voices(provider=...)`, then decorates the payload with
`VOICE_BLURBS` (voices.py) to mark featured voices. The new `mic:voice`
(no arg) inherits that path exactly — the payload shape is
`{"provider", "current", "featured", "all"}` today; the design keeps
those four fields and adds the standardised `available` alias for `all`
so the shape matches `mic:model` / `mic:provider`:

```json
{
  "provider": "elevenlabs",
  "current": "matilda",
  "available": ["matilda", "roger", ...],
  "featured": [{"name": "matilda", "blurb": "..."}, ...]
}
```

`available` and `all` carry the same list — `all` is retained so the
existing slash-command consumers keep working with no rename, and
`available` is added for uniformity with the two new tools. The next
release can drop `all` if consumers migrate; PL-PP-1 forbids doing so
in the same commit that introduces `available` only if code still
reads `all`. In practice the design retires `mic:who` and rewrites
every reader, so the CLI/slash consumers move to `available` in the
same commit and `all` is dropped from the reply — one field, uniform.

---

## 4. Decisions (resolved in this design)

### (a) One MCP tool per concern, no-arg = list

**Decision:** one tool per concern (`mic:model`, `mic:provider`,
`mic:voice`), each with `name?: str` signature. No `list_models` +
`set_model` split.

**Why.** The epic's target end-state table pins this shape, and it
matches every other read/write pair vox already exposes:
`mic:enablement(action)` (server_enablement.py:45), `mic:speak(mode)`
(server.py:651), `mic:notify(mode)` (server.py:603), and
`mic:music(subcommand, ...)` (server.py:553) all use one tool per
concern with a discriminator argument. Splitting into
`mic:list_models` + `mic:set_model` would double the tool surface for
no gain — the argument-absence discriminator is unambiguous (an MCP
schema declares `name` as optional, and the tool inspects it before
branching, the same shape `mic:enablement` uses with its `action`
Literal). One tool per concern also keeps the parity tests one-per-
concern instead of two-per-concern, and keeps `mic:who` and the future
tool-count budget under control (a large tool count is what MCP clients
have to page through in their tool picker).

### (b) Single-verb CLI, no `set`/`list` sub-sub

**Decision:** `vox model [<name>]`, `vox provider [<name>]`, `vox voice
[<name>]` — each is one Typer command, with an optional positional
argument, no sub-subcommands.

**Why.** cli.md §Subcommand naming: "Subcommands are **single verbs**:
`search`, `ingest`, `explain`, `talk`, `write`. Arguments disambiguate,
not subcommand names." The presence or absence of the `name` argument
is the disambiguator, exactly as `bd show <id>` and a hypothetical
`bd show` (no arg) would be one command. The alternative
(`vox model set`, `vox model list`) invents a subcommand where the
argument already carries the intent, and it does not match how `vox
voice` (with an argument today) or `vox voices` (a separate list
command today) actually read — the epic exists to end that split, not
to reproduce it under a new spelling.

### (c) `/vox:model` no-arg opens `AskUserQuestion`

**Decision:** the slash commands, when invoked with no argument, open
one `AskUserQuestion` dialog listing the available names. The user's
pick then drives a second `mic:<concern>` call with `name=<pick>`.
The dialog contract:

```yaml
questions:
  - question: "Which <concern>?"        # "Which model?", "Which provider?", "Which voice?"
    header: "<Concern>"                 # "Model", "Provider", "Voice" — capitalized noun
    multiSelect: false
    options:
      - label: <name>                   # the raw identifier the user picks
        description: <blurb-or-empty>   # for voice: from VOICE_BLURBS; for others: empty
        # the current selection's description is suffixed " (current)"
```

- For **model**: `options` is the list `mic:model` returned, blurbs empty.
- For **provider**: `options` is the fixed five-provider list, blurbs empty.
- For **voice**: `options` is the first 4 candidates the current
  `commands/unmute.md` builds (unmute.md:22) — current voice first,
  then the featured voices, deduped. Blurbs come from the reply's
  `featured` array.
- Options are capped at 4 for **voice** (the current cap
  commands/unmute.md:22 already enforces because AskUserQuestion is a
  short-list picker, not a browser). For **model** and **provider**
  the enum is small (≤5 for provider, ≤5 for elevenlabs models); no
  cap needed.

**Why.** Jim established, in the epic conversation, "when in claude
code, the user should get a choice dialog"; the epic's per-surface
end-state table calls the slash behaviour "AskUserQuestion picker with
the current selection marked". The current `/unmute` already uses this
shape (commands/unmute.md:22–23); this design generalises it across the
three concerns.

### (d) `mic:unmute voice=` remains a per-call override, not a session switch

**Decision:** after the strip, `mic:unmute` keeps a `voice` parameter
whose meaning is a per-call override on the ONE synthesis this call
triggers. It does not touch `SessionConfig._voice`. A call with only
`voice=` and no `text`/`segments` is a schema violation: `mic:unmute`
returns `{"error": "provide text or segments"}` (the current
server.py:449 return, unchanged) rather than silently updating the
session voice.

**Why.** cli.md §Projection Strategy: "each MCP tool = one engine
capability". `mic:unmute`'s capability is "synthesize and play"; a
per-call voice on a synthesis call is part of that capability (which
voice to synthesize with, for THIS call). Changing the session default
is a separate capability, owned by `mic:voice`. Callers wanting to
switch the session voice call `mic:voice`; callers wanting a one-shot
voice on a synthesis call still write it in `mic:unmute`'s `voice=`
(or per-segment `voice` in `segments`, server.py:404–408). The rejection
of the voice-only call is what makes the two capabilities visibly
different at the schema level.

**Note on state semantics.** This change moves one write path (a
session-voice update triggered by an argument-only `mic:unmute` call)
onto a different tool. It does not change the state model itself: the
session voice is one field, written by one code path (`SessionConfig.set_voice`
at server.py:215), and it will still be written by one code path after
the change — `mic:voice`'s handler will call the same setter. No z-spec
is warranted; the state field and its invariants are unchanged.

### (e) Deposited guide (`assets/global-guidance.md`) rewrites

Every section that mentions model, provider, voice, or the switch-tool
surface changes. The full list:

**Section "Speaking" (assets/global-guidance.md:32–44):** rewrite the
bullet list.

- Delete: `mic:who — list voices for the current provider (featured + full roster)` (line 42).
- Add before `mic:speak`:
  `mic:model [name] — switch TTS model (no arg lists available)`
  `mic:provider [name] — switch TTS provider (no arg lists elevenlabs/openai/polly/say/espeak)`
  `mic:voice [name] — set the session voice (no arg lists the roster, current marked)`
- Update the `mic:unmute` bullet (line 34) — remove the
  "vibe_tags/provider/model override" implicit meaning; state:
  `mic:unmute — synthesize and play text (or segments). Model, provider, and voice switches live on mic:model, mic:provider, mic:voice — never call unmute with only those set.`

**Section "Slash commands" (assets/global-guidance.md:116–125):** rewrite the list.

- Delete line 119: `/vox model <name> / /vox provider <name> — switch TTS engine mid-session.`
- Delete line 120–121:
  `/unmute [voice] — enable voice mode, optionally set the session voice; /unmute (no argument) browses the roster.`
- Add three new bullets (one per switch), and reword `/unmute`:
  `/vox:model [<name>] — switch TTS model (no arg picker).`
  `/vox:provider [<name>] — switch TTS provider (no arg picker).`
  `/vox:voice [<name>] — set session voice (no arg picker).`
  `/unmute — enable voice mode (spoken notifications).`
- Keep the `/vox enable`, `/vox disable`, `/mute`, `/vibe`, `/music`,
  `/recap` lines unchanged.

**Section "Driving vox from the CLI" (assets/global-guidance.md:127–143):**
rewrite the CLI bullet list.

- Delete line 138: `vox voice <name> — set the session voice; vox voices — list the roster.`
- Add: `vox model [<name>], vox provider [<name>], vox voice [<name>] — switch TTS model/provider/voice; no arg lists (--json for machine consumers).`
- Keep the other bullets unchanged.

The rewrite runs on both the source-of-truth
(`src/punt_vox/assets/global-guidance.md`) and, transitively via the
installer/upgrade path (`VoxGuidance` at guidance.py, referenced from
`__main__.py:670–674`), the deployed
`~/.punt-labs/vox/CLAUDE.md`. `vox install` and `vox enable` both
re-deposit the guide wholesale (tool-enable-disable.md §2.3), so a
fleet upgrade is one re-run.

---

## 5. Write-set per implementation unit

The four beads decompose into three implementation units after the
sequencing analysis in §6 collapses .1/.2/.3 into one unit (they
touch the same files in `server.py` and `commands/`, and splitting them
would triple the parity-test scaffolding and the guide rewrite). Unit
4 is the strip, which is hard-blocked on the switch tools existing.

### Unit A (vox-0rp9.1 + .2 + .3): add the three switch tools

**Creates:**

- `src/punt_vox/models.py` — the model enum table, `resolve_model()`, per §3.1.
- `src/punt_vox/server_switches.py` — three tool classes
  `ModelTool`, `ProviderTool`, `VoiceTool`, each with a `dispatch(name?)`
  method. Held apart from `server.py` so that module stays under the
  module-size threshold (PL-MD-1 / PY-OO-2), matching the pattern
  `server_audio_tools.py` and `server_music_tool.py` already use.
- `commands/model.md` — the `/vox:model` slash command.
- `commands/provider.md` — the `/vox:provider` slash command.
- `commands/voice.md` — the `/vox:voice` slash command.
- `tests/test_server_switches.py` — unit tests for the three tools,
  mirroring `tests/test_server_music_tool.py`.
- `tests/test_switches_surface_parity.py` — the parity harness (§5),
  mirroring `tests/test_music_surface_parity.py`.
- `tests/test_models.py` — shorthand resolution and provider→models mapping.

**Edits:**

- `src/punt_vox/server.py` — register the three new tools alongside
  `mic:music` and `mic:enablement` at the bottom of the tool block
  (server.py:553, 559). Delete the `mic:who` tool
  (server.py:562–600). Do **not** yet touch `mic:unmute` (unit B).
- `src/punt_vox/__main__.py` — add three `@app.command()` handlers
  (`model_cmd`, `provider_cmd`, `voice_cmd_new`) using the same
  formatter/JSON contract as the existing `voice_cmd` at
  `__main__.py:472–482`. Delete the old `voice_cmd` (line 472–482) and
  the `voices_cmd` (line 489–519); the new `voice_cmd_new` — renamed
  `voice_cmd` — replaces both, with the no-arg branch invoking the
  same list path `voices_cmd` uses today (`VoxClientSync().voices(...)`
  at `__main__.py:506` and the `_voices_text` renderer at
  `__main__.py:517`).
- `commands/vox.md` — delete the `model` and `provider`
  sub-sections (commands/vox.md:24–34 and commands/vox.md:36–40).
  Rewrite the Usage block (commands/vox.md:15–18) to list only
  `enable`/`disable`. Rewrite the fallback line
  (commands/vox.md:66) to name the three new slash commands.
- `commands/unmute.md` — delete the named-voice branch
  (commands/unmute.md:24) and the no-argument voice-picker branch
  (commands/unmute.md:20–23). Rewrite `/unmute` as a bare
  "enable-voice-mode" toggle: one `mic:speak(mode="y")` call, no
  picker, no `voice=` argument. Update the argument-hint frontmatter
  (commands/unmute.md:3) to remove `[voice-name]`. Remove
  `mcp__plugin_vox_mic__who` and `AskUserQuestion` from
  `allowed-tools` (commands/unmute.md:4).
- `src/punt_vox/assets/global-guidance.md` — the rewrites in §4e.
- `README.md` — update any user-visible mention of `/vox model`,
  `/vox provider`, `vox voices`, `mic:who`, or `/unmute matilda` to
  the new spelling. The reader-visible commands table (search for
  `/vox model` / `mic:who` / `vox voices`) is the load-bearing change.
- `CHANGELOG.md` — one `[Unreleased]` entry under `Changed`, naming
  every retired form and its replacement.
- `tests/test_server.py` — delete the `mic:who` test suite
  (test_server.py:847–930 — the class starting `# who tool tests`).
- `tests/test_cli.py` — replace `voices` and old-`voice` tests with the
  new `vox voice [<name>]` behaviour tests. Delete the "errors on no
  arg" test the current `vox voice` has (`__main__.py:479`).
- `hooks/session-start.sh` — if it prunes retired top-level slash
  commands (as it did for `/enable`/`/disable` per CHANGELOG.md 4.17.0),
  add `model.md`, `provider.md`, `voice.md` to the DEPLOY list; nothing
  to prune, since these are new files.

**Deletes:**

- `src/punt_vox/server.py` `mic:who` tool (server.py:562–600).
- `src/punt_vox/__main__.py` `vox voices` command
  (`__main__.py:489–519`) and the `_voices_text` helper
  (`__main__.py:517–519`) if not reused.

### Unit B (vox-0rp9.4): strip `mic:unmute` overloads

**Edits (no new files):**

- `src/punt_vox/server.py` `unmute` function (server.py:374–487):
  - Delete the `provider: str | None = None` and `model: str | None = None`
    parameters (server.py:388–389).
  - Delete the pre-synthesis persist-to-session block (server.py:436–441).
  - Delete the `given` / `updates` overload branch (server.py:444–449).
  - Update the docstring (server.py:391–427) to name the new dedicated
    tools and remove the `provider` / `model` argument descriptions.
  - Update the tool description string on `mcp` registration
    (server.py:82–95) to remove the overload hint.
- `src/punt_vox/server.py` `notify` function (server.py:603–648):
  - Delete the `voice: str | None = None` parameter (server.py:606).
  - Delete the `stored_voice = _session.set_voice(voice)` and the
    `if stored_voice is not None:` update-injection
    (server.py:641–643).
  - Update the docstring to remove `voice` mention.
- `src/punt_vox/server.py` `speak` function (server.py:651–680): same
  strip as `notify` above.
- `src/punt_vox/cli_enablement.py` `notify` method
  (cli_enablement.py:123–153):
  - Delete the `voice: _VoiceOpt = None` parameter
    (cli_enablement.py:126) and the `_resolve_voice` helper
    (cli_enablement.py:155–163) if it becomes unused.
  - Delete the `stored_voice = self._resolve_voice(voice)` block
    (cli_enablement.py:149–151).
  - Update the docstring.
- `tests/test_server.py` — delete tests that assert the `mic:unmute`
  `provider=`/`model=`/`voice=` overload writes to session
  (search for `provider=` / `model=` / `voice=` in `mic:unmute` tests).
  Add a test:
  `test_unmute_rejects_provider_or_model_kwargs` — a MCP client
  passing `provider=` or `model=` to `mic:unmute` is a schema violation
  (the argument does not exist), verified by FastMCP's tool-schema
  introspection.
- `tests/test_cli_enablement.py` — delete the `--voice` test on
  `vox notify`. The switch belongs to `vox voice` now.
- `tests/test_server.py` — delete the `voice=` tests on `notify` and
  `speak`. Add a test that the tools reject an unknown keyword arg
  (FastMCP's schema layer).
- `src/punt_vox/assets/global-guidance.md` — the `mic:unmute` bullet
  rewrite from §4e lands in unit B (unit A's edit lists the tools, unit
  B updates the description that says "don't call unmute with only
  switch fields"). The two edits do not conflict — unit A adds the
  three new tool bullets, unit B rewrites the existing `mic:unmute` bullet.
- Any internal caller of `mic:unmute` that passes `provider=`,
  `model=`, or a voice-only call — search `src/punt_vox/` for
  `unmute(` and `mic__unmute` — is rewritten to call the new dedicated
  tool. The recap hook and the deposited guide are the two candidates;
  in the recap hook (commands/recap.md:19–21) the call passes `text=`
  and `ephemeral=`, not switch fields, so no change. In the deposited
  guide, §4e handles it.
- `CHANGELOG.md` — extend unit A's `Changed` entry (or add a sibling
  entry) naming the `mic:unmute` schema tightening as a **breaking
  change** — a caller passing `provider=`/`model=` will now be rejected.

**Deletes:** none new — the strip is in-place edits.

### Unit C (optional, follow-up): the Lux Control Panel adds provider/model pickers

Not in this design — vox-0rp9.12 covers the panel work and is
sequenced after the three switch tools land. Called out here only
because the panel currently reads and writes `voice` through the
same config file (`.punt-labs/vox/vox.md`) — the switch tools
retain that write path (`ConfigStore.write_field`) unchanged, so the
panel's voice combo keeps working without any panel change.

---

## 6. Sequencing

```text
     ┌──────────────────────┐
     │ PR #396 (vox-0rp9.5) │        (already in review — mic:notify off-mode retirement)
     └──────────┬───────────┘
                │  merges to main
                ▼
     ┌──────────────────────┐
     │ Unit A               │        (vox-0rp9.1 + .2 + .3 — add mic:model/provider/voice)
     └──────────┬───────────┘
                │  merges to main
                ▼
     ┌──────────────────────┐
     │ Unit B               │        (vox-0rp9.4 — strip mic:unmute overloads)
     └──────────────────────┘
```

**Why one unit for .1/.2/.3, not three.** The three switch tools share
one `server_switches.py` module, one deposited-guide rewrite, one
parity-test file, and one docstring for the tool description. Splitting
them into three PRs would force the guide and the parity harness to
land in exactly one of the three, with the other two carrying half-written
scaffolding — or would duplicate the scaffolding three times. The
rollback boundary is one boundary: "the three switch tools work" or
"they don't". They rollback together.

**Why Unit B is separate from Unit A.** Unit B removes a public
schema (the `mic:unmute` `provider=`, `model=`, and voice-only paths).
If Unit A ships but Unit B ships broken, agents keep using the old
overload and the epic delivers half the value — but nothing breaks.
If Unit A+B ship together and Unit B is broken, an agent's
`mic:unmute(text="hello")` call could fail (a schema-strip bug that
rejects `text=` alongside `provider=`), taking synthesis down with
the strip. Splitting them isolates a synthesis regression from the new
tools' rollout and lets the strip get its own review lens
(silent-failure-hunter is especially cheap to run on Unit B, whose
whole job is "reject harder").

**Rollback coherence.** Unit A can revert alone — the strip has not
happened, so the old overload paths still work. Unit B revert brings
back the overloads on top of Unit A's tools — no functional loss.

**Dispatch order after PR #396 merges.**

1. Dispatch Unit A (design mission → implementation mission with the
   Unit-A write-set from §5). One PR.
2. When Unit A merges, dispatch Unit B. One PR.

Neither unit is blocked on anything but the previous merge. Nothing in
this design is blocked on a decision that is still open.

---

## 7. Test strategy

### 7.1 Parity harness (`tests/test_switches_surface_parity.py`)

One file, following the shape of `tests/test_music_surface_parity.py`
line for line. Both surfaces drive the same in-memory fake:

- A `_FakeVoxdClient` (mirroring the shape of `_FailingClient` at
  test_server.py:111) with `voices(provider) -> list[str]`,
  `health() -> HealthStatus`. For `mic:model` and `mic:provider`, no
  daemon call is needed — the enum is static — so the fake is only
  wired for `voice`.
- A `_FakeConfigStore` capturing writes so both surfaces can be asked
  "what did you write?" and compared.

Tests, one per concern, one per verb:

- `test_model_list_reports_the_same_fields` — no-arg call on both
  surfaces returns the same `{available, current}` after CLI-only key
  rename (§2.3).
- `test_model_set_writes_the_same_field` — `name=v3` on both surfaces
  writes `model=eleven_v3` to the fake `ConfigStore`. Shorthand resolves
  identically.
- `test_provider_list_reports_the_same_fields` — same shape.
- `test_provider_set_writes_the_same_field` — `name=openai` writes
  `provider=openai`.
- `test_voice_list_reports_the_same_fields` — includes `featured` on
  both surfaces (the payload the picker uses).
- `test_voice_set_writes_the_same_field` — `name=matilda` writes
  `voice=matilda`; a leading `@` is stripped the same way on both
  (the `SynthesisSpec.normalize_voice` choke-point).
- `test_all_three_surfaces_expose_the_same_verbs` — the CLI `app`'s
  `registered_commands` includes `model`, `provider`, `voice`; the
  MCP `mcp` registered tools include `mic:model`, `mic:provider`,
  `mic:voice`.

The parity file has no assertions about prose — the CLI's human
messages differ from the tool's returned JSON; only the field set and
its values are compared.

### 7.2 Schema-introspection tests (`tests/test_switches_schema.py`)

FastMCP builds each tool's schema from the Python signature. Assert:

- `mic:model.inputSchema.properties.name` type is `string`,
  `required` is empty (name is optional). Assert absence of any
  `provider` or `model` sibling.
- `mic:provider.inputSchema.properties.name.enum ==
  ["elevenlabs", "openai", "polly", "say", "espeak"]` — the Literal
  narrows to the exact five (per §3.2).
- `mic:voice.inputSchema.properties.name` type is `string`, no enum
  (the voice roster is provider-specific and lookup-time, not
  schema-time).
- After Unit B: `mic:unmute.inputSchema.properties` contains **no**
  `provider` and **no** `model` keys. `voice` remains.
- After Unit B: `mic:notify.inputSchema.properties` contains **no**
  `voice` key. Same for `mic:speak`.

### 7.3 Tests deleted

- `tests/test_server.py`: the `mic:who` tool suite (test_server.py:847–930).
- `tests/test_server.py`: any `mic:unmute` test whose scenario is
  "call with provider= and no text" or "call with model= and no
  text" — the current server.py:445–448 behaviour that returns
  `{"status": "config updated", ...}`.
- `tests/test_server.py`: `notify`/`speak` tests that pass `voice=`.
- `tests/test_cli.py`: the `voices` command tests.
- `tests/test_cli.py`: the `voice` command "errors on no arg" test.

Every deletion travels in the same commit as the code delete, per PY-RF-6.

### 7.4 Coverage floor

The parity file + schema file + a small unit-tests file for
`punt_vox.models` add ≈15 tests. `pytest --co -q` count strictly
increases across the two PRs (PL-TT-2). The parity-file assertions
each carry one meaning ("this field is on both surfaces"), one
assertion each where reasonable (PL-TT-3).

---

## 8. Retirement inventory

Every retired form deletes in the same commit as its replacement
lands. No form gets an alias, a `_deprecated` wrapper, a
`removed-in-vX.Y` shim, or a `warnings.warn` bridge, per PL-PP-1.

| Retired form | Location | Replacement | Retires in |
|---|---|---|---|
| `/vox model <name>` slash sub-verb | commands/vox.md:15, 24–34 | `/vox:model [<name>]` (new `commands/model.md`) | Unit A |
| `/vox provider <name>` slash sub-verb | commands/vox.md:16, 36–40 | `/vox:provider [<name>]` (new `commands/provider.md`) | Unit A |
| `/unmute [voice]` argument | commands/unmute.md:20–24 | `/vox:voice [<name>]` (new `commands/voice.md`); `/unmute` retains its no-arg toggle-on role | Unit A |
| `vox voice <name>` errors on no arg | `__main__.py:472–482` | extended `vox voice [<name>]` — no arg lists | Unit A |
| `vox voices` separate command | `__main__.py:489–519` | folded into `vox voice` no arg | Unit A |
| `mic:who` tool | server.py:562–600 | folded into `mic:voice` no arg | Unit A |
| `mic:unmute(provider=)` overload | server.py:388, 438–439 | `mic:provider(name=)` | Unit B |
| `mic:unmute(model=)` overload | server.py:389, 438–439 | `mic:model(name=)` | Unit B |
| `mic:unmute(voice=)`-only overload (voice with no text) | server.py:377, implicit today | `mic:voice(name=)`; the `mic:unmute` `voice=` remains as a per-call synthesis override | Unit B |
| `mic:notify(voice=)` overload | server.py:606, 641–643 | `mic:voice(name=)` | Unit B |
| `mic:speak(voice=)` overload | server.py:654, 672–675 | `mic:voice(name=)` | Unit B |
| `vox notify --voice` flag | `cli_enablement.py:126, 149–151` | `vox voice <name>` (separate call) | Unit B |
| Guide bullet: `mic:who — list voices…` | assets/global-guidance.md:42 | `mic:voice [name] — set / no arg lists` | Unit A |
| Guide bullet: `/vox model <name>` and `/vox provider <name>` slash lines | assets/global-guidance.md:119 | `/vox:model`, `/vox:provider`, `/vox:voice` | Unit A |
| Guide bullet: `/unmute [voice]` slash line | assets/global-guidance.md:120–121 | `/unmute` (bare) + `/vox:voice` | Unit A |
| Guide bullet: `vox voice <name> — set…; vox voices — list` | assets/global-guidance.md:138 | `vox model`, `vox provider`, `vox voice` — one bullet | Unit A |

The commands/vox.md file survives, reduced to `enable`/`disable` and
their usage — the only two verbs it should ever have owned.

---

## 9. Constraints (for the leader's review)

- **No migration/compat/shim.** The design contains none; a leader
  finding one must strike it. Deposited-guide upgrades happen wholesale
  via the existing `VoxGuidance` re-deposit path (tool-enable-disable.md
  §2.3), not through a version-detect shim.
- **`make check` on every commit.** The commit boundary in §5 sets
  the granularity — the guide rewrite ships with the code that
  matches it, the tests ship with the code they test, no commit is
  green-on-code-broken-on-docs or vice versa.
- **OO ratchet.** `server_switches.py` adds new classes (three tools,
  matching `MusicTool`/`EnablementTool` shape) rather than growing
  `server.py`, which improves `module_size` and `classes_per_module`
  on the touched files (PY-OO-2). `punt_vox/models.py` adds one
  domain class carrying the shorthand table, retiring three
  scattered constants (PL-CO-3 module cohesion).
- **Coverage never decreases.** §7.4.
- **No z-spec.** §Preamble states why: no state machine changes.
- **PY-EH-8.** The switch tools' write path never returns `None`
  on failure — an unknown provider returns
  `{"error": "..."}`, an unreachable daemon returns
  `{"error": "..."}`, both matching every other tool's
  `_error(...)` helper (server.py:364–366). The CLI raises
  `typer.BadParameter` on an unknown shorthand and `typer.Exit(code=1)`
  on a daemon fault, matching `__main__.py:284–286`.

---

## 10. Deferred (not this design)

- **vox-0rp9.12** — Vox Control Panel gets provider/model pickers. The
  panel currently exposes only voice; adding pickers is a lux applet
  change, not a surface/schema change. The switch tools land first;
  the panel adds pickers in a follow-on PR. This design keeps the
  `ConfigStore.write_field` choke-point so the panel's existing voice
  combo keeps working through the transition.
- **Model listing that talks to the daemon.** ElevenLabs exposes a
  models API; today vox hard-codes the four models the code path
  supports (elevenlabs.py:39–43) with per-model token limits. A
  runtime-discovered list would need daemon plumbing and provider
  interface work; the design deliberately keeps the enum static
  (§3.1) so this PR does not couple to that.
- **Provider auto-detect surfacing.** The `providers/__init__.py`
  auto-detection today picks a provider based on API keys and platform
  (`providers/__init__.py:128–169`); the `mic:provider` tool overrides
  it. The design does not expose the auto-detected "why" in the tool
  reply — that would be a `mic:status` extension, not a switch tool
  concern.
