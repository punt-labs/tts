# Audio Programs — Phase 1.5: the music command-surface tidy

**Status:** Design for implementation. Author: Raymond H (rmh), 2026-07-25.
Scope: the music command surface only — no Program/Part state-machine change, so
no z-spec. Get music right first; podcast (Phase 2) and audiobook (Phase 3) copy
this shape.

## What this fixes

The music internals are already format-agnostic (`voxd/programs/`). The *edges*
are not. Four inconsistencies, one per operator decision (2026-07-25):

| Problem | Today | Target |
|---|---|---|
| Authored input | `on` builds a `PromptSet`; `new` sends a bare `str` | one `PromptSet`, both verbs, both surfaces |
| MCP surface | one `music` tool (`mode=`) **plus** `music_play`/`music_new`/`music_next`/`music_list`/`music_get`/`music_remove` | one `music` tool, `subcommand` argument |
| CLI authoring | no `on` verb — only the MCP tool can author a pool | `cat pool.json \| vox music on`, stdin, no `--file` |
| `--json` position | `vox music list --json` is rejected (vox-cnak) | `--json` works in every position |

The mapping rule that ties them together: **`vox <group> <subcommand>` maps to
the MCP tool `<group>` with its first argument `<subcommand>`.** It is uniform,
so `rec` (and later `podcast`/`audiobook`) fold to the same shape.

---

## 1. The shared `PromptSet` — one authored-input type, both surfaces

Show the type first. It already exists and is already dependency-free (stdlib
only), importable by the thin client layer without pulling in the daemon
(`types_programs/prompts.py:27-113`):

```python
@dataclass(frozen=True, slots=True)
class PromptSet:
    base: str
    variations: tuple[str, ...]   # () => single/fallback; 12 => agent pool
```

`variations` empty means "use `base` for every track"; twelve entries means
"track `i` uses `base` + `variations[i]`" (`prompt_for`, `prompts.py:104-113`).
That single rule already spans both the pool case and the single-track case — so
it is the right type for `new` as well as `on`.

### The one wire form both surfaces produce and the daemon consumes

```json
{ "base_prompt": "<str>", "variations": ["<str>", ...] }
```

`variations` absent or empty ⇒ single/fallback. `PromptSet.from_wire`
(`prompts.py:40-56`) is the single parser for this shape on the daemon side.

### Builders — one per surface entry, none hand-rolls a dict

| Builder | Caller | Status |
|---|---|---|
| `PromptSet.from_tool_args(base_prompt, variations)` | MCP `music on` | exists (`prompts.py:58-69`) |
| `PromptSet.from_wire(msg)` | CLI `music on` (stdin JSON), daemon parse | exists (`prompts.py:40-56`) |
| `PromptSet.from_agent(base, variations)` | validation funnel (exactly 12) | exists (`prompts.py:71-90`) |
| `PromptSet.fallback(style, mood)` | hook-driven vibe change (decorated literal) | exists (`prompts.py:92-102`) |
| **`PromptSet.single(prompt)`** — **NEW** | `music new`, both surfaces | to add |

`single` is the missing piece for `new`. It wraps one verbatim prompt as a
base-only set, kept distinct from `fallback` because `new` must send the
prompt **untouched** — no `". instrumental, loopable."` decoration:

```python
@classmethod
def single(cls, prompt: str) -> Self:
    """Return a one-track set: the verbatim prompt as base, no variations."""
    clean = prompt.strip()
    if not clean:
        msg = "prompt must be a non-empty string"
        raise ValueError(msg)
    return cls(base=clean, variations=())
```

### How `music new` uses it (today it sends a bare string)

Today `new` is a bare `str` on both surfaces:

- CLI: `MusicCli.new` takes `prompt: str` and calls
  `self._catalog_factory().new(prompt, name)` (`cli_music.py:141-156`, the call
  at `:155`).
- MCP: `MusicCatalogTools.new(prompt, name)` calls `music_new(prompt, name)`
  (`server_audio_tools.py:248-263`, the call at `:260`).
- Wire: `VoxClientSync.music_new(self, prompt: str, name)` sends `prompt`
  (`client_sync.py:235-237`); the daemon reads it as `parse_optional_str(msg,
  "prompt")` and generates one track (`library_handlers.py:51-72`, the parse at
  `:61`).

Target: both surfaces build `PromptSet.single(prompt)` and the catalog path
carries the `PromptSet`, forward-integrated end to end.

- `CatalogGateway.new(self, prompts: PromptSet, name)` (was `prompt: str`,
  `catalog_gateway.py:22-24`).
- `ClientCatalogGateway.new` forwards `prompts` to `music_new`
  (`client_catalog_gateway.py:33-35`).
- `VoxClientSync.music_new(self, prompts: PromptSet, name)` sends the wire form
  `base_prompt` (+ empty `variations`) instead of `prompt` (`client_sync.py:235`).
- `MusicNewHandler.__call__` parses `PromptSet.from_wire(msg)`, requires a
  non-`None` base, and generates `prompt_for(0)` (`library_handlers.py:51-72`).
  The wire key renames `prompt` → `base_prompt`; the bare-string path is deleted.

No compatibility shim: the `prompt` wire key and the `str` signatures are
removed in the same change, every caller updated (PL-PP-1, PY-RF-6).

**Rejected alternative:** keep the `music_new` wire as a bare `str` and have the
surfaces build a `PromptSet` only to extract `.base` at the client boundary. It
is weaker — the daemon never receives the authored-input object, so the
"one object both surfaces build **and send**" invariant (operator decision 1)
does not hold, and podcast/audiobook would inherit a split where the CLI/MCP
build an object the wire discards. Send the object.

---

## 2. The single `music` MCP tool

One tool, `subcommand` as the first argument, collapsing the seven music tools
that exist today: `music` (`server.py:556-606`), `music_play`
(`server.py:609-651`), `music_list` (`server.py:654-684`), `music_next`
(`server.py:687-700`), and the three catalog registrations `music_new`/
`music_get`/`music_remove` (`server.py:519-521`).

### Schema

```python
def dispatch(
    self,
    subcommand: str,                       # on|off|play|next|new|list|get|remove
    style: str | None = None,
    vibe: str | None = None,
    name: str | None = None,
    album_id: str | None = None,
    base_prompt: str | None = None,
    variations: list[str] | None = None,
    dest: str | None = None,
) -> str: ...
```

Registered as `mcp.tool(name="music")(_music_tool.dispatch)` — FastMCP builds
the schema from the signature exactly as it does for the bound-method tools
today (`server.py:514-521`). The first argument is named `subcommand`, not
`mode`, because the name is uniform across every group — `rec`'s first argument
is `subcommand` too, and `mode` is music-specific.

### Parameters per subcommand

| `subcommand` | Reads | Gateway op |
|---|---|---|
| `on` | `style`, `name`, `base_prompt`, `variations` (vibe from session) | `program.start(StartRequest)` |
| `off` | — | `program.stop()` |
| `play` | `style`, `vibe`, `name`, `album_id` | `program.select(SelectionRequest)` |
| `next` | — | `program.advance()` |
| `list` | — | `program.catalog()` |
| `new` | `base_prompt`, `name` | `catalog.new(PromptSet.single, name)` |
| `get` | `album_id`, `dest` | `catalog.get(album_id, dest)` |
| `remove` | `album_id` | `catalog.remove(album_id)` |

`base_prompt` carries the authored base for **both** `on` (with 12 `variations`)
and `new` (single track, no `variations`) — the shared `PromptSet` shape from
§1, so the tool never grows a second prompt parameter.

### Dispatch discipline — a table, not an `if`-ladder

The subcommand selects a handler through an explicit dispatch table, not a
conditional forest (polymorphism over conditionals, `oo.md`; PY-OO-6). Raw tool
arguments bundle into a frozen `MusicArgs` value object (PY-OO-3 param bundling);
each handler reads only the fields it needs:

```python
@final
class MusicTool:
    __slots__ = (
        "_program_factory", "_catalog_factory",
        "_session_provider", "_marquee", "_pref",
    )

    def dispatch(self, subcommand: str, *, ...) -> str:
        self._session_provider().refresh_from_config()
        args = MusicArgs(subcommand, style, vibe, name, album_id,
                         base_prompt, variations, dest)
        handler = self._HANDLERS.get(subcommand)
        if handler is None:
            return _error(f"unknown music subcommand: {subcommand!r}")
        return handler(self, args)

    _HANDLERS: ClassVar[dict[str, Callable[[MusicTool, MusicArgs], str]]] = {
        "on": _on, "off": _off, "play": _play, "next": _advance,
        "list": _list, "new": _new, "get": _get, "remove": _remove,
    }
```

The table is a literal of the class's own methods — an explicit map, not
`getattr`-by-name (PY-TS-11 forbids introspective dispatch). Each `_on`/`_play`/…
holds exactly the logic that lives in the corresponding module function today
(the marquee line, the `MusicPreference` confirm, the `SelectionRequest`
resolved-style round-trip); it is moved, not rewritten.

`MusicTool` holds **both** gateway factories, the session provider (a
`lambda: _session` closure, as `RecTools` does — `server.py:511`), the
`MusicMarquee`, and the `MusicPreference`. It therefore absorbs
`MusicCatalogTools` (`server_audio_tools.py:228-289`) entirely — `new`/`get`/
`remove` become its handlers — and that class is deleted.

### The return contract is unchanged

Every subcommand still returns the same JSON it returns today — `{"message",
"applied"}` for the control/playback verbs, `{"message", "programs"}` for
`list`, `{"album_id"}`/`{"path"}`/`{"removed"}` for the catalog verbs, and the
`{"error": ...}` envelope on a daemon fault or malformed prompt. Control actions
(`on`/`off`/`play`/`next`) still emit no agent prose; the JSON drives the panel
only (the settled control-action-no-text behavior).

### Where it lives

New module `src/punt_vox/server_music_tool.py` holding `MusicTool` + `MusicArgs`
(one abstraction per module; keeps `server.py` and `server_audio_tools.py` under
the module-size and class-count thresholds, mirroring how `server_audio_tools.py`
was split out). `server.py` imports it, instantiates one `_music_tool`, and
registers the single tool.

---

## 3. Folding `rec` to the same shape

**Recommendation: music folds in this pass (the reference); `rec` folds in an
immediate fast-follow PR — the `--json` CLI parity for `rec` ships now.**

The mapping rule is uniform, so `rec` gets the identical treatment: one `rec`
tool with `subcommand` ∈ `{new, list, play, get, remove}`, replacing the five
bare registrations `rec_new`/`rec_list`/`rec_play`/`rec_get`/`rec_remove`
(`server.py:514-518`). The implementation is mechanical once `MusicTool` proves
the pattern: `RecTool` (`server_audio_tools.py`'s `RecTools` reshaped to a
`dispatch` + a `RecArgs` bundle) is the same move.

Why a fast-follow and not the same PR:

1. **Rollback coherence.** The music tidy — `PromptSet` unification, the single
   `music` tool, `vox music on` stdin, and the `music`/`rec` `--json` parity — is
   one revertible unit. The `rec` **MCP-tool collapse** reverts independently of
   the music surface; nothing in music depends on it.
2. **Prove the reference before copying it.** WORKFLOW's Phase-1.5 rationale is
   "get music right so podcast and audiobook copy a clean pattern." The same
   applies to `rec`: land `MusicTool`, review it, confirm the audio demo, then
   apply the proven `dispatch`/`Args` shape to `rec` with zero design risk.
3. **One mission = one task.** The music implementation mission stays focused;
   the `rec` collapse is its own one-task mission against the now-settled pattern.

What ships **now**, not in the fast-follow: the `rec` **CLI** `--json` parity
(§4). `vox rec list --json` is rejected today for the identical vox-cnak reason,
and the fix is the same one-line `OutputFlags` wiring already being applied to
`music`. Leaving it would be shipping a known bug — there is no "pre-existing"
excuse. Only the `rec` MCP-surface reshape defers.

The fast-follow's full write set (turnkey): `RecTool` + `RecArgs` in
`server_audio_tools.py`, the single `mcp.tool(name="rec")` registration in
`server.py`, deletion of the five `rec_*` registrations, and the
`commands/` and guidance-doc updates for the `mic:rec subcommand=` shape.

---

## 4. `vox music on` — CLI stdin wiring

`vox music on` does not exist today; `build_music_app` wires only
`new`/`list`/`play`/`off`/`get`/`remove`/`next`/`status` (`cli_music.py:217-232`).
Add it as the CLI twin of `music(subcommand="on")`.

The authored pool arrives on **stdin** as the wire-form JSON of §1 — no `--file`
flag (operator decision 3):

```bash
cat pool.json | vox music on --style trance
```

`pool.json` is `{"base_prompt": "...", "variations": ["...", ...12...]}`.

Wiring:

```python
def on(
    self,
    style: _StyleTag = None,
    name: _NameOpt = None,
    *, json_output=..., verbose=..., quiet=...,
) -> None:
    self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
    prompts = self._guard(self._read_pool)          # PromptSet | None
    request = StartRequest(
        style=style, vibe=self._vibe_source(), name=name, prompts=prompts,
    )
    outcome = self._guard(lambda: self._gateway_factory().start(request))
    self._formatter.emit(
        {"music": "on", "applied": outcome.applied},
        outcome.display("Generating music."),
    )

def _read_pool(self) -> PromptSet | None:
    """Return the stdin pool as a PromptSet, or None when nothing is piped."""
    if sys.stdin.isatty():          # interactive, no pipe -> daemon fallback
        return None
    raw = sys.stdin.read().strip()
    if not raw:
        return None
    return PromptSet.from_wire(json.loads(raw))
```

Details:

- **Piped ⇒ parse; tty ⇒ fallback.** The `isatty()` gate is the same Unix
  convention `TextInput._should_read_stdin` uses (`cli_io.py:102-111`): a
  pipeline supplies the pool, an interactive `vox music on` with no pipe sends
  `prompts=None` and the daemon uses its minimal literal fallback (`StartRequest.
  prompts=None` is the documented "absence" contract — `control.py:20-33`).
- **Malformed pool is a clean CLI error.** `json.loads` (`ValueError`) and
  `PromptSet.from_wire` → `from_agent` (wrong variation count / blank entries,
  `ValueError` — `prompts.py:71-90`) both run inside `self._guard`, whose
  `_GATEWAY_ERRORS` already includes `ValueError` (`cli_music.py:32-38`,
  `78-88`). Same behavior as the MCP tool surfacing the `ValueError` at its
  boundary.
- **vibe from config.** `_vibe_source` reads
  `ConfigStore(find_config_dir() or DEFAULT_CONFIG_DIR).read().vibe`, so the CLI
  sends the same `StartRequest.vibe` the MCP tool sends from `_session.vibe`
  (`server.py:589-591`). It is injected as a `Callable[[], str | None]` seam so a
  test pins it, matching the existing `gateway_factory`/`catalog_factory` seams.

`new` changes in the same file: build `PromptSet.single(prompt)` and call
`self._catalog_factory().new(prompts, name)` (§1). The positional stays a bare
verbatim prompt argument — a single track has nothing to pipe.

---

## 5. `--json` parity (vox-cnak)

`vox music list --json` is rejected because the music sub-app commands do not
declare `--json`/`--verbose`/`--quiet`. The flags live only on the top-level
callback (`__main__.py:247-255`), so they parse **before** the subcommand
(`vox --json music list`) but not **after** it — Typer sees `--json` as an
unknown option on the `list` command and errors.

The state-emitting top-level commands already solve this: `say`, `voices`,
`status`, `version` each redeclare the three flags and fold them in through the
shared `OutputFlags.apply(...)`, which ORs both positions together
(`__main__.py:513-538` is the template; `cli_io.py:48-63` is the mechanism).
The music (and `rec`) sub-apps simply never got the same wiring.

Fix: give **every** music subcommand the three flags and route them through the
one shared `OutputFlags` (operator decision 4 — "every position"). That instance
is a module global in `__main__` (`_flags`, `__main__.py:75`) and must be handed
to the sub-app so the subcommands can reach it:

- `build_music_app(formatter, flags)` — `MusicCli.__new__` gains
  `flags: OutputFlags` (`cli_music.py:50-60`, `217-232`).
- Each verb gains `*, json_output=False, verbose=False, quiet=False` and a
  leading `self._flags.apply(json_output=..., verbose=..., quiet=...)` before it
  emits. The bodies already emit through `self._formatter`, which `OutputFlags`
  flips into JSON mode — so no body logic changes, only the flag plumbing.
- Same wiring for `rec`: `build_rec_app(formatter, flags)` and the flags on
  `RecCli`'s verbs (`cli_rec.py:333-345`). This is the `rec` CLI parity that
  ships now (§3).
- `__main__` passes `_flags` into both: `build_music_app(_formatter, _flags)`
  and `build_rec_app(_formatter, _flags)` (`__main__.py:914`, `:921`).

Shared `_JsonOutput`/`_Verbose`/`_Quiet` `Annotated` aliases in `cli_music.py`
keep the per-verb signatures terse, the way `cli_rec.py` already defines its own
option aliases (`cli_rec.py:113-153`).

---

## 6. Write set

Forward integration throughout — every rename/collapse deletes the old path and
updates all callers in the same change; no shims, no aliases (PL-PP-1, PY-RF-6).

### Create

| Path | Contents |
|---|---|
| `src/punt_vox/server_music_tool.py` | `MusicTool` (`dispatch` + 8 handlers) and `MusicArgs` frozen value object |
| `tests/test_server_music_tool.py` | per-subcommand dispatch (fake program + catalog gateways), unknown-subcommand error, malformed-prompt error, catalog vs program isolation |

### Change

| Path | Change |
|---|---|
| `src/punt_vox/types_programs/prompts.py` | add `PromptSet.single(prompt)` classmethod (`__all__` unchanged) |
| `src/punt_vox/server.py` | delete `music`/`music_play`/`music_list`/`music_next` functions (`:556-700`) and the `music_new`/`music_get`/`music_remove` registrations (`:519-521`); drop the `MusicCatalogTools` import (`:32`); construct one `MusicTool` and register `mcp.tool(name="music")(_music_tool.dispatch)` |
| `src/punt_vox/server_audio_tools.py` | delete `MusicCatalogTools` (`:228-289`, moved into `MusicTool`); keep `RecTools`; update module docstring |
| `src/punt_vox/catalog_gateway.py` | `new(self, prompts: PromptSet, name)` |
| `src/punt_vox/client_catalog_gateway.py` | `new` forwards `prompts` to `music_new` |
| `src/punt_vox/client_sync.py` | `music_new(self, prompts: PromptSet, name)`; send `base_prompt` wire key (`:235-237`) |
| `src/punt_vox/voxd/programs/library_handlers.py` | `MusicNewHandler.__call__` parses `PromptSet.from_wire`, requires base, generates `prompt_for(0)`; wire key `prompt` → `base_prompt` (`:51-72`) |
| `src/punt_vox/cli_music.py` | add `on` verb + `_read_pool` + `_vibe_source`; `new` builds `PromptSet.single`; `--json`/`--verbose`/`--quiet` on every verb via injected `OutputFlags`; `MusicCli.__new__` gains `flags` + `vibe_source`; `build_music_app(formatter, flags)`; register `on` |
| `src/punt_vox/cli_rec.py` | `--json`/`--verbose`/`--quiet` on the state-emitting verbs via injected `OutputFlags`; `RecCli.__new__` gains `flags`; `build_rec_app(formatter, flags)` |
| `src/punt_vox/__main__.py` | `build_music_app(_formatter, _flags)`, `build_rec_app(_formatter, _flags)` (`:914`, `:921`) |
| `commands/music.md`, `/music` slash-command wiring | `mic:music mode=` → `mic:music subcommand=`; document the eight subcommands and the stdin pool for `vox music on` |
| tests | `test_prompts.py` (`single`), `test_cli_music.py` (`on` stdin + fallback + malformed, `new` PromptSet, `--json` in both positions), `test_cli_rec.py` (`--json` parity), `test_server.py` (single `music` tool dispatch), `test_client_sync.py` (`music_new` wire), `test_library_handlers.py` (`base_prompt` parse), `test_catalog_gateway`/`test_client_catalog_gateway` (`PromptSet` signature) |

### Delete (forward integration)

- `MusicCatalogTools` (`server_audio_tools.py:228-289`).
- `music`, `music_play`, `music_list`, `music_next` module functions and the
  three `music_*` bare registrations in `server.py`.
- The bare-string `prompt` path: the `str` parameter on `CatalogGateway.new`,
  `ClientCatalogGateway.new`, `VoxClientSync.music_new`, and the `prompt` wire
  key in `MusicNewHandler`.

### Deferred to the immediate fast-follow (separate mission/PR)

- The `rec` **MCP-tool** collapse: `RecTool` + `RecArgs`, one `mcp.tool(name=
  "rec")` registration, deletion of the five `rec_*` registrations, and the
  `commands/`/guidance updates for `mic:rec subcommand=`. (The `rec` **CLI**
  `--json` parity ships in this pass.)

### For the COO (doc, not code)

- `~/.punt-labs/vox/CLAUDE.md` / user-facing guidance that references
  `mic:music mode=` — update to `subcommand=` after merge.
- `CHANGELOG.md` (`## [Unreleased]`), `README.md` (the new `vox music on` verb
  and stdin pool), and `DESIGN.md` (an ADR for the uniform group/subcommand tool
  shape).

---

## 7. OO ratchet

The change pays debt down rather than adding it: collapsing four module-level
functions in `server.py` plus the `MusicCatalogTools` class into one cohesive
`MusicTool` raises `method_ratio` and `class_to_func_ratio` and shrinks
`server.py`'s module size, while the dispatch table replaces the per-tool
conditional bodies with polymorphic handlers. `MusicArgs` retires the
seven-loose-parameter smell on the tool signature (PY-OO-3). No metric regresses;
several improve. Run `make update-oo` after `make check` is green and stage
`.oo-baseline.json` + `.oo-audit.jsonl` with the implementation commit.

## 8. Tests

- `PromptSet.single`: non-empty base wraps to `(base, ())`; blank raises.
- `music on` MCP + CLI: 12-variation pool, fallback (`None`) path, malformed pool
  (`ValueError` → clean surface error), start request carries the session/config
  vibe.
- `music new` both surfaces: `PromptSet.single` reaches the catalog gateway; the
  `base_prompt` wire round-trips; the daemon generates `prompt_for(0)`.
- Single `music` tool: each `subcommand` dispatches to the right op; unknown
  subcommand returns `{"error"}`; catalog verbs leave the active Program
  untouched while playback verbs never touch the catalog.
- `--json` parity: `vox music list --json` and `vox --json music list` both emit
  JSON; likewise `vox rec list --json`.

Each modeled property is asserted by name so a later refactor cannot silently
drop it.
</content>
</invoke>
