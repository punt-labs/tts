# Audio Programs — Albums, Tags & Catalog (vox-q7vh)

**Status:** RATIFIED — model + all decisions LOCKED (operator, 2026-07-08); design +
formal model complete and reviewed. Ready for implementation. Author: Claude (COO).
Bead: vox-q7vh (direction A). Builds on the shipped Phase-1 Program model (DES-041,
`docs/audio-programs.tex`).

## The reframe (locked)

Per-vibe pools is not a directory-layout problem. The manifest already stores the
pool's identity as data, and voxd keeps no catalog in memory (it re-scans manifests
per `/music list`, and `resolve(name)` reads one manifest *by directory name* — the
coupling to remove). So:

> **manifest = source of truth · catalog = in-memory index over manifests ·
> directory = dumb, collision-proof `<slug>-<id>` storage.**

The by-genre / by-vibe "structure" lives in the catalog as tag queries, never in the
folder tree.

## Album identity: id + tags (LOCKED)

- Every **album** (a Program: up to 12 tracks) has its own **unique id**.
- `style` and `vibe` are **queryable tags on the album, not a key.** An **arbitrary
  number** of albums is supported, **including many albums that share the same
  `(style, vibe)`** — the arbitrary-albums lock is about the *tag* axes only. The tag
  pair is a **named record** — `style`/`vibe` are named fields, not positional — so this
  doc's `(style, vibe)` and the Z model's (and legacy `subject`'s) `(vibe, style)` denote
  the same named pair; prose order is presentational, not a positional contract.
- `name` is the album's **unique handle** — a separate, enforced-unique axis (R5,
  operator 2026-07-08): no two albums share a `name`, so `by_name(name)` returns 0 or
  1, and `--name X` resolves to the one album named `X` (resume if it exists, else
  mint a fresh album — auto-suffixed to `X1`/`X2`/… if the desired name collides).
  Style/vibe are the non-unique tag axes; name is the unique-handle axis. `name` stays
  optional: an album minted without `--name` carries no curated handle and is
  addressed purely by tags.
- Directory = **`<slug>-<id>/`** — the slug (`<style>--<vibe>` or the curated name) is
  a cosmetic Finder prefix; the short id guarantees uniqueness at arbitrary scale.
  `manifest.json` is authoritative — directory names are never parsed back, so a slug
  collision is harmless.
- Single-segment directory names keep this repo's path-traversal guard
  (`ProgramName` rejects path separators and the special `.` and `..` path
  components -- not names that merely contain a dot) intact.

### Manifest schema (the locked target shape — replaces the old `subject{vibe,style}`)

```jsonc
{
  "id": "a3f1c9",
  "format": "playlist",
  "tags": { "style": "trance", "vibe": "calm", "name": null },
  "created": "2026-07-08T02:14:07+00:00",
  "prompt_fingerprint": "9f2a7c31",
  "parts": [
    { "index": 1,  "file": "001.mp3", "status": "ready", "duration_ms": 182000 },
    // …
    { "index": 12, "file": "012.mp3", "status": "ready", "duration_ms": 190200 }
  ]
}
```

`created` is a **tz-aware UTC** timestamp serialized via `datetime.isoformat()`, which
emits the numeric-offset form `+00:00` (e.g. `2026-07-08T02:14:07+00:00`) — *not* a
trailing `Z` — and parsed back via `datetime.fromisoformat`, which yields a tz-aware
value. Being tz-aware end-to-end is load-bearing: a naive `datetime.now()` would
make `newest`'s comparisons raise `TypeError` against an aware value (finding #6). It
enables "resume the newest matching album." `name`, when present, is unique across
albums (R5); `tags.name == null` means an unnamed, tag-addressed album.

`prompt_fingerprint` is a **stable hash of the authored prompt-set** (base prompt +
the 12 variations) that produced the album — a hidden metadata field, *not* a
user-facing `style`/`vibe` tag. It makes a pool self-identify which prompt-set it
was generated from, so `/music on` can refuse to fill a pool with tracks authored
from a different prompt-set (resolves vox-1uo5 — see below).

## Directory listing (the locked layout)

```text
~/Music/vox/
├── trance--calm-a3f1c9/            id=a3f1c9  tags{style:trance, vibe:calm}
│   ├── 001.mp3 … 012.mp3
│   └── manifest.json
├── trance--calm-7b2e04/            id=7b2e04  tags{style:trance, vibe:calm}   ← 2nd, same tags
│   ├── 001.mp3 … 012.mp3
│   └── manifest.json
├── trance--energetic-4c8b11/       id=4c8b11  tags{style:trance, vibe:energetic}
│   └── … manifest.json
├── lofi--calm-1f9a30/              id=1f9a30  tags{style:lofi, vibe:calm}     ← 9/12, still filling
│   ├── 001.mp3 … 009.mp3
│   └── manifest.json
├── klezmer--celebratory-e20d77/    id=e20d77  tags{style:klezmer, vibe:celebratory}
│   └── … manifest.json
├── focus-beats-c40d11/             id=c40d11  tags{name:focus-beats, style:lofi, vibe:focused}
│   └── … manifest.json
└── trance/                         LEGACY (pre-change): no id, manifest.subject.vibe=="trance";
    └── … manifest.json               ignored by tag matching. No migration.
```

## The catalog (in-memory, new)

Built once at startup by parsing every manifest; updated on generation. Replaces the
per-list disk scan. It is the sole object list / play / switch talk to. Queries:

- `by_id(id)`, `by_name(name)` → one album
- `by_tags(style?, vibe?)` → the set of matching albums, `created`-descending
- `newest(style, vibe)` → the most recent album matching the tags
- `newest(style, vibe, fingerprint)` → the most recent match **whose prompt
  fingerprint equals `fingerprint`** — the `/music on` resume path (resolves
  vox-1uo5). Resume matches on `(style, vibe, fingerprint)`: a `(style, vibe)` hit
  with a *different* fingerprint is a miss, so resume mints a fresh album rather than
  filling an existing pool. (`--name` still binds by name regardless of fingerprint.)

## Prompt fingerprint — one pool, one prompt-set (resolves vox-1uo5)

**Problem (vox-1uo5).** An agent's authored prompt-set can change between sessions.
Resuming "the newest `(style, vibe)` album" and filling it with tracks generated from
a *different* prompt-set produces one pool that mixes old and new material — an
incoherent album.

**Fix (option (b), operator 2026-07-08).** Every album records a **prompt
fingerprint**: a stable hash of the authored prompt-set (base prompt + the 12
variations). `/music on` resolution matches the newest album with the same
`(style, vibe)` **AND the same fingerprint**. If the incoming prompt-set does not
match any pool's fingerprint, resolution **mints a NEW album (fresh id)** rather than
filling into an existing one. A pool therefore always reflects exactly one coherent
authored prompt-set — the old/new mix cannot occur, because different prompts resolve
to a different album.

**Interactions:**

1. **Resume-vs-create.** The fingerprint joins `(style, vibe)` (and `name`) as a
   `catalog.bind`/`turn_on` match key. A tag match with a *different* fingerprint is a
   miss → mint fresh, not fill.
2. **Storage.** The fingerprint is written to the manifest (`prompt_fingerprint`), so
   a resumed album's stored fingerprint can be compared against the incoming
   prompt-set's computed fingerprint.
3. **Name-addressed resume still wins by name.** A `--name X` resume binds the album
   named `X` **regardless of fingerprint** — a curated name is the handle. If the
   incoming prompt-set differs from a resumed named album's stored fingerprint, the
   design **keeps playing the named pool** (no warn, no forced regenerate): the user
   named it, so the name governs. Documented here so the divergence from the tag path
   is deliberate, not accidental.

## Playback: tag-filter → Selection → rotate

Every selector is a tag filter over the catalog producing a **Selection** (an ordered
track list). Single-match = one album; multi-match = a **union radio**.

> **Ordered list vs. set (F#4).** The *implementation* carries a `Selection` as an
> ordered `tuple[SelectedPart, ...]` for deterministic rotation, but the *Z model*
> defines `Selection == ℙ PART` — a set, opaque membership. The ordering is an
> implementation detail below the Z abstraction: the model reasons only over set
> membership and cardinality (`playing ∈ selection`, no-immediate-repeat by
> `#selection`), which the ordered tuple satisfies exactly. No model change — the
> model stays a set; the tuple is the refinement that realizes it.

| Query | Resolves to |
|---|---|
| `play focus-beats` | album `c40d11` (name) |
| `play trance calm` | union `a3f1c9` + `7b2e04` (two albums share the tags) |
| `play trance` | all trance albums |
| `play calm` | all calm albums (cross-genre) |
| `/music on` (no args) | current style + **current session vibe** → newest album matching those tags **and the incoming prompt fingerprint** (vox-1uo5), generate/mint fresh if none |

Two playback modes:

- **Active Program (generate).** A single album, ≤ 12, vibe-adaptive; `/music on`
  generates + rotates + switches album on a `/vibe` change. **Phase 1, unchanged** —
  only *which* album it binds to is now a catalog lookup.
- **Replay a Selection (consume-only).** Any Selection — name, `(style,vibe)`,
  style-union, vibe-union — shuffle-rotated. No generation, no 12-cap, no vibe-switch.

**`/music on` (LOCKED):** with no parameters, defaults to the current style and the
**current session vibe** — that pair is the selection. It resolves to the newest album
matching those tags **and the incoming prompt fingerprint** (vox-1uo5) and generates a
fresh one only if none matches — a `(style, vibe)` hit with a *different* fingerprint is
a miss, so it mints fresh rather than filling a foreign pool. An explicit `style …` or
`--name …` overrides the corresponding tag; the `--name` path binds by name regardless
of fingerprint (a named pool keeps playing).

## The three implementation moves

1. **Record the real vibe as a tag.** `_subject_for` today writes the *style* into
   the vibe field and drops the session vibe — fix it to record `(style, vibe)` tags.
2. **Filter by tags, not resolve-by-name.** `/music on` and the replay surfaces query
   the catalog by tags. *This is the feature.*
3. **Free the directory name** to `<slug>-<id>`; identity is in the manifest.

## No migration

Pre-change directories (no `id`, `subject.vibe == style`) coexist as legacy dirs;
they never match a tag query and are deletable by hand. No converter, no legacy
detector (org no-migration rule).

## Resolved decisions (operator, 2026-07-08)

- **P3 — vibe normalization: NOT handled at the music level.** The album's `vibe`
  tag is the session vibe as-is. `/vibe auto` already constrains the vocabulary
  upstream — `mood.py` buckets into `MOOD_FAMILIES` (bright/dark/neutral) and auto
  mode derives the mood from a fixed signal-trajectory table (~6 outcomes). No
  music-layer canonicalization, and no further constraints for now. If coarser
  bucketing is ever wanted, it belongs to the vibe system, not the album layer.
- **M1 — formal model: model the replay Selection as a no-generation rotation.** It
  is the same shape as the existing Rotate transition with generation disabled, over
  a Selection that may span multiple albums. The active-Program transitions are
  otherwise unchanged (binding an album is a catalog lookup, not a new transition).
  jms includes the Selection rotation in `docs/audio-programs.tex` at design time.

## Next

Spec is ratified; the design (manifest / catalog / selection decomposition + write
set) and the Selection/Radio Z model are complete and reviewed. Next is
implementation, per the two-phase delegation model.

## Design (decomposition, write set, test plan)

**Author:** Raymond H (rmh), 2026-07-07. Design phase for vox-q7vh. No production
code — this section is the write set the implementation mission builds from.
**Revised 2026-07-08** to fold in the gvr + rej design reviews (converged) and the
operator's R5 ruling. See **Design-review resolutions** at the end for the
per-finding disposition; the tables, invariants, Z-delta, and test plan below are
already updated to those resolutions.

### The one hard decision, stated first

The reframe is cheap everywhere except one place: **union radio replay can exceed
12 tracks and spans multiple album directories**, and the shipped Phase-1
`ProgramState` is capped at `poolSize` (12) with one active directory. The spec
locks both facts: replay has *no 12-cap*, and the Phase-1 machine is *unchanged*.
Those two locks together forbid shoehorning union replay through `ProgramState`.

So the design splits playback into two sources behind one Protocol:

- **`Program`** (unchanged) — the generate mode: one album, `ProgramState`, ≤ 12,
  filling, vibe-adaptive. Every S1–S16 invariant survives verbatim because the
  entity is not touched.
- **`SelectionPlayback`** (new) — the consume-only replay: a flat shuffle-rotation
  over a `Selection` that may span albums and directories, no cap, no fill, no
  generation.

Both satisfy a new `PlaybackSource` Protocol the loop and channel animate. This is
the central layering change and the one item most needing the leader's review
(risk R1 below). The rejected alternative — relax `ProgramState`'s cap so a
Selection is "just a big Program" — violates the "Phase-1 unchanged" lock and the
`#pool ≤ poolSize` invariant, and would let a 24-track union corrupt every mode
implication (`playingRotating ⟹ #pool = poolSize`). Rejected.

### Layer map (dependency arrow points inward)

```text
identity/value  album_id · album_tags · selection · part · identifiers
      │
domain          manifest · catalog · program (unchanged) · selection_playback
      │            state · invariants · rotate_policy · playback_source (Protocol)
      │
persistence     store (Protocol) · filesystem_store
      │
orchestration   service · control_channel · loop · switch_signal · lifecycle_signal
      │
wire handlers   on/off/next/play/select/list/status handlers · wiring
      │
client/present  program_gateway · client_gateway · program_control
                cli_music · server (music tools)
```

### New modules (the write set — create)

| Module | Type(s) | Responsibility |
|---|---|---|
| `voxd/programs/album_id.py` | `AlbumId` | A short unique hex id (`secrets.token_hex(3)`). Validates, parses, `__eq__`/`__hash__`. **`AlbumId.mint(taken)` owns the collision loop** — the id value-space is `AlbumId`'s, so the loop lives here and `Catalog.mint_id()` delegates to it (finding #8). |
| `voxd/programs/prompt_fingerprint.py` | `PromptFingerprint` | A stable hash of the authored prompt-set (base prompt + the 12 variations). `PromptFingerprint.from_prompts(base, variations) -> PromptFingerprint` canonicalizes (order-stable join) then hashes (e.g. `sha256`, truncated hex); validates, parses, `__eq__`/`__hash__`. A frozen value — hidden album metadata, never a user-facing tag (vox-1uo5). The fingerprint value-space is this type's; `Catalog.bind`/`newest` compare `PromptFingerprint`, not raw strings. |
| `voxd/programs/album_tags.py` | `AlbumTags`, `TagQuery` | `AlbumTags` = `style`, `vibe`, `name: str \| None`; `slug() -> str` (the `<style>--<vibe>` or curated-`name` Finder prefix), wire round-trip. **`TagQuery`** (`style`, `vibe`, `name` — each `str \| None`) owns `matches(tags: AlbumTags) -> bool`; it replaces the repeated `(str\|None, str\|None)` tuple across `by_tags`/`newest`/`by_name` (finding #7, PY-OO-7). **`AlbumTags.mint_unique_name(desired, taken)`** returns a name absent from `taken`, auto-suffixing `X`→`X1`/`X2`/… — the name-space mint parallel to `AlbumId.mint` (R5). The slug rule stays behaviour on the tags, not a free function. |
| `voxd/programs/catalog.py` | `Catalog`, `Album` | `Album` = `(manifest, locator)` where **`locator` is opaque — the `AlbumId` or directory-name string, never a live `Path`** (finding #3; the store dereferences locator→`Path`). `Catalog` = in-memory index built once from `Album`s: `by_id`, `by_name` (0 or 1 — names unique), `by_tags(query)` newest-first, `newest(query)`, `add(album)`, `mint_id()` (→ `AlbumId.mint`). **Resolution lives here, not in the service** (finding #11): `bind(request) -> Album` (name→tags+fingerprint→mint) and `select(query: TagQuery) -> Selection`. **`select` operates on the tag filter only; an `id` is served by the direct `by_id(id)` lookup, never routed through `select`/`TagQuery` (F#7)** — the handler picks `by_id` vs. `select` before calling the catalog. `bind`'s tag path matches `newest(style, vibe, fingerprint)` — a `(style, vibe)` hit with a *different* `PromptFingerprint` is a miss, so it mints a fresh album rather than filling a pool with a foreign prompt-set (vox-1uo5). The `--name` path binds by name regardless of fingerprint (a named pool keeps playing). The sole object list/play/switch consult. |
| `voxd/programs/selection.py` | `Selection`, `SelectedPart` | `SelectedPart` = `(part, locator)` — an **opaque locator** (the album's `AlbumId` or directory-name string, same discipline as `Part.identity`), **not a `Path`** (finding #3), so `selection.py` never imports `pathlib`. `Selection` = ordered `tuple[SelectedPart, ...]` from one or more albums, `from_albums(...)`, `__len__`, `__bool__`. |
| `voxd/programs/selection_playback.py` | `SelectionPlayback` | Consume-only rotation cursor over a `Selection`: `playing`, `last_played`, `rotate()`, `wants_generation -> bool` (structurally `False`), **`is_playing -> bool` (returns `playing is not None`, F#6 — lets the loop auto-advance a radio on track-end)**. **Anti-repeat is delegated to the injected `PlaybackPolicy.next_part` (`RotatePolicy`)** — the same strategy that backs `Program.rotate`, so "no immediate repeat" is defined once (finding #9). No cap, no fill, no generation, **no `locate`** (path resolution is the persistence seam's job, finding #2). The replay half of `PlaybackSource`. |
| `voxd/programs/playback_source.py` | `PlaybackSource` (Protocol) | The narrowed structural interface the loop/channel animate: `playing -> Part \| None`, `rotate() -> None`, **`wants_generation -> bool`** (finding #1), **`is_playing -> bool`** (F#6 — the source-agnostic advance gate). No `filling`, no `locate` (finding #2). `Program` (adapted) returns `state.filling or mode is RETRYING` for `wants_generation` and `mode in {the playing modes}` for `is_playing` (preserving today's mode gate verbatim); `SelectionPlayback` returns `False` for `wants_generation` and `playing is not None` for `is_playing`. |
| `voxd/programs/select_signal.py` | `SwitchSelection` | The consume-only switch signal, verb-parallel to `SwitchProgram` (finding #13 — `SwitchProgram`/`SwitchSelection` share the `Switch*` verb, leaving the existing signal name untouched): retarget the channel to a `SelectionPlayback` over a resolved `Selection`, interrupts, begins at the first track. No fill reconcile arms. |

### Modified modules (the write set — change)

| Module | Change |
|---|---|
| `manifest.py` | Replace `subject: PlaylistSubject` with `id: AlbumId`, `tags: AlbumTags`, `created: datetime` (tz-aware UTC), `prompt_fingerprint: PromptFingerprint` (vox-1uo5 — hidden metadata, round-trips as hex). **Delete `PlaylistSubject` outright** (no shim — PL-PP-1; its `(vibe, style)` role is subsumed by `AlbumTags`). `from_json` **stays total** (finding #5): it requires `id`/`tags`/`created` and raises on a malformed record (PY-EH-8) — the "raises only when the caller demands an album" clause is struck, because it is not expressible on a total `from_json`. Add **`ProgramManifest.from_wire(obj)`** so the store's `scan()` can peek `opt_str("id")` and skip idless legacy dirs *before* committing to a full parse. `created` round-trips as `datetime.isoformat()` / `datetime.fromisoformat`. Add **`ManifestDraft`** (`id`, `tags`, `prompt_fingerprint`, `parts` — no `created`): the store stamps `created` (see finding #6) and persists the caller-supplied fingerprint. Drop the `name: ProgramName` *identity* field. |
| `identifiers.py` | Keep `ProgramName` but re-document it as **the on-disk directory component only** (`<slug>-<id>`) — the path-traversal guard is exactly what a derived dir name still needs. It is no longer a user-facing identity. `PartRef`, `Reason` unchanged. |
| `store.py` (Protocol) | Replace name-addressed `list_programs`/`resolve`/`open(name)` with: `scan() -> tuple[Album, ...]` (startup catalog build, the only disk walk, skipping idless legacy dirs), `open(directory) -> PartStore`, `create(draft: ManifestDraft) -> PartStore`. **The store owns the clock** (finding #6): `create` stamps `created = datetime.now(UTC)` and writes the full manifest — the caller hands a `ManifestDraft`, never a `created` value. **`open`-guard invariant (finding #10, hardened by F#1):** `open` only ever receives a scan/create-*validated* path — `scan()` resolves-and-contains-checks every candidate against the programs root (rejecting symlinks and escapes) and `create()` composes only a validated `<slug>-<id>` segment — so no wire/CLI path, and no symlinked child under root, can hand `open` a directory that resolves outside root. A passed PY-EH-1 boundary. `resolve(name)` is **deleted** (the coupling the spec names). |
| `filesystem_store.py` | Implement the new protocol: `scan` globs `*/manifest.json`, **resolves each candidate directory (`Path.resolve()`) and confirms the resolved path is a real directory whose resolved path is contained under the programs root** — rejecting symlinks and any child that escapes root (F#1) — then **peeks `opt_str("id")` via `from_wire` and skips idless legacy dirs** (debug-logged, finding #5), pairing survivors with their directory into an `Album`. `create` composes only a validated single `tags.slug() + "-" + id` segment via `ProgramName`, stamps `created = datetime.now(UTC)`, keeps the path guard, and uses **`mkdir(exist_ok=False)` as the second-line race guard** (finding #8 — `AlbumId.mint` is first-line). `_program_dir` takes a directory, not a name. It is the seam that **dereferences an opaque locator → `Path`** (finding #3). |
| `service.py` | An **orchestrator, not an algorithm** (finding #11). Hold a `Catalog` built from `store.scan()` at construction. `turn_on(style, vibe, name, prompts)` computes `PromptFingerprint.from_prompts(prompts)` and includes it in the bind request, then `catalog.bind(request) -> Album` → seed a `Program` → post `SwitchProgram`. New `replay(query)` → `catalog.select(query) -> Selection` → post `SwitchSelection`. The service **does not** accrete `_bind_album`/`_tags_for`/`_subject_for` — that resolution lives on `Catalog`/`AlbumTags`, so the service only seeds a source and posts a signal (god-object risk averted). The session vibe is recorded as the real `vibe` tag (move #1). |
| `active_context.py` | `ActiveProgram` unchanged for generate. **`locate(part) -> Path` lives here, on the `active_context`/`PlayerDirectory` seam** (finding #2): the seam has two shapes — the generate context returns its single directory for any part; a selection locator resolves each `SelectedPart`'s opaque locator to a `Path` under root (via the store). The single writer swaps which shape is active alongside the source retarget. `active_directory() -> Path` widens to `locate(part) -> Path`. |
| `control_channel.py` | `_program: Program` → `_source: PlaybackSource`; `program`/`retarget` generalise to `source`/`retarget(source)`. **`_apply_one` passes `self._source` to `signal.apply(source)`** (finding #4). Fill reconcile reads `source.wants_generation` (finding #1) — `False` for a Selection, so it cancels and idles with no special-casing. |
| `fill_reconciler.py` | `reconcile(program)` → **`reconcile(source: PlaybackSource)`** reading `source.wants_generation` (finding #1). This preserves the load-bearing `OR mode is RETRYING` clause verbatim: `Program.wants_generation` returns `state.filling or mode is RETRYING`, so a transient backoff still keeps the retry engine running (the vox-ig52 stranded-retry fix). A bare `filling` would drop that clause. |
| `control_signal.py` (Protocol) + fill-outcome family (`fill_outcome.py`, `fill_signal.py`) | `apply(self, program, /)` → **`apply(self, source: PlaybackSource, /)`** (finding #4). Each generate-family signal (the fill outcomes `RecoveringFillOutcome`/transient `FillSignal`, and any Program-only transition) narrows `isinstance(source, Program)`; when the active source is a `SelectionPlayback` it **rejects as a lost race via `GuardViolationError.reject(...)`** — the INFO-logged, writer-survives path. This closes the bas7-class crash: a fill task that posts `Produced` *after* a `SwitchSelection` retargeted to a `SelectionPlayback` (which has no `fill_ok`) drops cleanly instead of raising `AttributeError`. `RecoveringFillOutcome.origin` widens to `PlaybackSource`; a stale generate outcome against a Selection drops. |
| `loop.py` | Reads `source.playing` and posts `Rotate`; the player resolves the dir via the **`PlayerDirectory.locate(target)`** seam (active-context side, finding #2), not via the source. **Generalizes the advance gate (F#6) — NOT "otherwise unchanged":** the shipped loop posts `Rotate` after a natural track-end only when `program.mode` is a playing mode, but a `SelectionPlayback` has no `mode`, so a radio would never auto-advance. The gate becomes source-agnostic — the loop checks **`source.is_playing`** (a new `PlaybackSource` member) instead of `program.mode in playing_modes`, so a Selection auto-advances on track-end exactly as a Program does. The interrupt/natural-end race is otherwise unchanged. |
| `player_directory.py` (Protocol) | `active_directory() -> Path` → **`locate(part: Part) -> Path`** (finding #2) so union replay resolves per-track; single-album generate returns its one dir for any part. |
| `switch_signal.py` | `SwitchProgram` retargets to a `Program` source (generate). Sibling `SwitchSelection` (new module) retargets to a `SelectionPlayback` (finding #13 — shared `Switch*` verb). |
| `on_handler.py` | Parse the new `vibe` wire field; pass to `service.turn_on`. |
| `play_handler.py` → `select_handler.py` | Replace directory-name replay with tag/name/id replay, driving `service.replay`. **Route by resolution kind (F#7): if `id` is present → a direct `catalog.by_id(id)` lookup (one album); else build a `TagQuery(style?, vibe?, name?)` → `catalog.select`.** `id` is a direct-lookup axis, distinct from the tag filter — do not shoehorn it into `TagQuery`, which stays style/vibe/name only. Rename the module to name what it does (select a Selection), delete the old name-addressed handler. |
| `list_handler.py` | Emit tag-rich rows: `id`, `style`, `vibe`, `name`, `ready`, `total`, `created` — from the catalog, not a re-scan. |
| `wiring.py` | Swap `program_play` → `program_select`; register against the catalog-backed service. |
| `program_control.py` | `StartRequest` gains `vibe: str \| None`. New `SelectionRequest(style?, vibe?, name?, id?)` — **`id` is a direct-lookup axis served by `catalog.by_id`, distinct from the `style`/`vibe`/`name` tag axes that build a `TagQuery` (F#7); it is never folded into `TagQuery`**. `ProgramSummary` gains `id`, `style`, `vibe`, `name`. |
| `program_gateway.py` / `client_gateway.py` | `start` carries `vibe`; `play`/`loop` → `select(SelectionRequest)`; `catalog` rows carry tags. |
| `cli_music.py` | `list` renders tags (grouped by style/vibe); `play`/`loop` take `style`/`vibe`/`--name` and query via the gateway; **stop constructing `FilesystemProgramStore` client-side** — the daemon owns the catalog (layering fix, risk R2). |
| `server.py` (`music` tool) | Resolve `vibe = _session.vibe` and pass `StartRequest(style, vibe, name, prompts)`. `music_play` → a select tool taking style/vibe/name. |

### Signal behavior by active source (F#2)

`ControlSignal.apply(source, /)` dispatches against whichever `PlaybackSource` the
channel currently holds. The behavior splits into three categories — *user-intent*,
*vibe-adaptive*, and *generate-internal* — and **only the generate-internal family is
rejected-as-lost-race** against a `SelectionPlayback`. The user-intent signals are
valid against *either* source; a `TurnOff` under replay is a stop, not a reject.

| Signal | Category | vs. active `Program` | vs. active `SelectionPlayback` (replay) |
|---|---|---|---|
| `TurnOff` | user-intent | stops — retarget to no source / off | **stops** — retarget to off. Valid retarget, *not* a lost-race reject. |
| `TurnOn` / `SwitchProgram` (enter/switch generate) | user-intent | (re)binds / retargets a `Program` | **switches to a `Program`** — retarget to generate mode. Valid. |
| `Next` | user-intent | `Rotate` the pool | **`RadioRotate`** — advance the `Selection` cursor. Valid against either. |
| `SwitchSelection` | user-intent | retarget to a `SelectionPlayback` | retarget to a new `SelectionPlayback`. Valid. |
| `VibeStyleChange` | vibe-adaptive (auto) | swaps the bound album for the new `(style,vibe)` | **ignored — a deliberate `ΞSelectionPlayback` no-op.** Replay is consume-only with *no vibe-switch* (spec lock); a drifting session vibe must not abandon a curated Selection. Distinct from a lost-race reject — nothing is "lost," the signal simply does not apply to replay. |
| fill outcomes (`Produced` / `RecoveringFillOutcome` / transient `FillSignal`) | generate-internal | apply to the `Program` | **lost-race no-op** — `isinstance(source, Program)` narrow fails; reject via `GuardViolationError` (INFO-logged, writer survives, finding #4). |

The dividing line: **user-intent signals (`off`, `on`, `switch`, `next`) act through the
`PlaybackSource` abstraction and are valid against either source** — they retarget the
channel (`TurnOff` → off, `TurnOn`/`SwitchProgram` → a `Program`, `SwitchSelection` → a
`SelectionPlayback`) or advance the cursor (`Next` → `RadioRotate`), never narrowing on
the concrete type. **Generate-internal signals narrow `isinstance(source, Program)`** and
reject as a lost race when the source is a `SelectionPlayback` — the bas7-class crash
defense (finding #4). **`VibeStyleChange` is the lone third case**: it retargets a live
`Program` but is a deliberate no-op against a `SelectionPlayback`, because replay carries
no vibe adaptation. `ControlSignal.apply` therefore has exactly three shapes — retarget
(user-intent); a lost-race **reject** for generate-internal outcomes (the
`isinstance(source, Program)` guard fails and *raises* `GuardViolationError`); and an
**ignore** for `VibeStyleChange` under replay (a total no-op that changes nothing and
never raises). Reject and ignore both leave state unchanged, but they are distinct
mechanisms — a raise vs. a total no-op — not one "guard-disabled" case. Each signal's
`apply` narrows (or does not) per its row above.

### Invariant preservation

**Phase-1 `ProgramState` (S1–S16): untouched.** The generate path still seeds a
`Program` over a single catalog album whose `ready_parts()` is ≤ 12 (a manifest
holds ≤ `poolSize` parts by construction). "Which album" is a *resolution* — a
catalog `by_tags`/`by_name` lookup producing `diskPool?` — exactly the shape of
finding #7's part-index resolution: it happens before the transition, never inside
it. `TurnOn`, `VibeStyleChange`, `FillOk`, `filling ⟹ mode`, `failed ⟹ pool = ∅`,
`playing ∈ pool` all hold because no transition, guard, or field changed.

**New invariants the design adds:**

- **`AlbumId` unique.** `AlbumId.mint(taken)` loops until the id is absent from the
  taken set (finding #8, first-line); `create` uses `mkdir(exist_ok=False)` so even a
  directory race regenerates (second-line).
- **`name` unique (R5).** No two albums share a `name`. `AlbumTags.mint_unique_name`
  auto-suffixes a colliding desired name at mint time, so `by_name(name)` returns 0
  or 1. Style/vibe stay non-unique.
- **One pool, one prompt-set (vox-1uo5).** Every album stores a `PromptFingerprint`
  of the prompt-set that authored it. `catalog.bind`'s tag path resumes only a pool
  whose fingerprint equals the incoming prompt-set's; a `(style,vibe)` match with a
  different fingerprint mints a fresh album. A pool therefore never mixes tracks from
  two prompt-sets. (`--name` resume is exempt — a named pool is the handle.)
- **A `Selection` never generates.** `SelectionPlayback` has no producer, no fill;
  `PlaybackSource.wants_generation` returns `False` structurally (finding #1).
- **A stale generate outcome against a Selection is a no-op (finding #4).** A fill
  outcome applied while the active source is a `SelectionPlayback` narrows
  `isinstance(source, Program)`, fails, and rejects as a `GuardViolationError` — the
  benign, INFO-logged lost race — instead of raising `AttributeError`. This mirrors
  the shape of `FillOk` being disabled outside `playing_filling`.
- **`playing ∈ selection`.** `SelectionPlayback.rotate()` chooses only from its
  `Selection`; the cursor cannot point outside it (mirrors S4 `playing ⊆ pool`).
- **No immediate repeat in replay** when `#selection ≥ 2`; single-track replays.
  Enforced by reusing `RotatePolicy.next_part` (finding #9) — the same strategy that
  backs `Program.rotate`, so the rule is defined once (mirrors Rotate's
  `#pool ≥ 2 ⟹ playing' ≠ playing`).
- **The loop auto-advances a `Selection` identically to a `Program` (F#6).** The
  advance gate is source-agnostic: the loop posts `Rotate` on a natural track-end
  whenever `source.is_playing`, which `Program` defines as `mode in {the playing
  modes}` (today's mode gate verbatim) and `SelectionPlayback` defines as `playing is
  not None`. A radio therefore auto-advances on track-end exactly as a generate pool
  does — a `SelectionPlayback` has no `mode`, so a mode-typed gate would have left it
  stuck on its first track.
- **`created` is tz-aware UTC (finding #6).** The store stamps `datetime.now(UTC)`;
  the value round-trips through `isoformat`/`fromisoformat`, so `newest`'s comparisons
  never raise `TypeError` on a naive/aware mismatch.
- **Catalog excludes legacy.** Idless dirs are skipped at the `scan()` boundary
  (`from_wire` peeks `opt_str("id")`), absent from every query, so a legacy
  `subject.vibe == style` dir can never satisfy `by_tags` (the spec's "never match").
- **`open` is a trusted boundary (finding #10, hardened by F#1).** `open(directory)`
  only ever receives a scan/create-*validated* path: `scan()` resolves every candidate
  (`Path.resolve()`) and confirms it is a real directory whose resolved path is
  contained under the programs root — rejecting symlinks and escapes — and `create()`
  composes only a validated `<slug>-<id>` single segment. The traversal defense is thus
  "scan resolves-and-contains-checks every candidate; open only ever receives a
  scan/create-validated path." The path-traversal guard the Phase-1 `ProgramName`
  provided is **preserved by this containment check, not lost**, now that the directory
  name is derived rather than user-supplied — a filesystem glob alone would let a
  symlinked child resolve outside root, so the resolve-and-contain check is what
  restores the guarantee. A passed PY-EH-1 boundary.

### Catalog build and legacy handling

At service construction, `store.scan()` walks `*/manifest.json` once, pairing each
manifest with its directory. Idless (pre-change) manifests are skipped with a debug
log — no converter, no legacy detector beyond "has an `id`?" (no-migration rule).
The catalog is authoritative thereafter; generation calls `catalog.add(album)` so a
freshly created album is queryable without a re-scan. Legacy dirs are invisible to
vox (Finder-only, deletable by hand) — recommend *not* surfacing them in `list`
(risk R3, a minor decision for the leader).

### `/music on` resolution (the locked default path)

1. `server.music(mode="on")` reads `style` (arg, else persisted) and
   `vibe = _session.vibe` (current session mood), passes both in `StartRequest`.
2. `service.turn_on(style, vibe, name, prompts)` computes
   `fingerprint = PromptFingerprint.from_prompts(prompts)`:
   - `name` present → `catalog.by_name(name)`. On a **hit**, resume it (name overrides
     vibe *and* fingerprint — a named pool keeps playing regardless of prompt-set
     drift, vox-1uo5). On a **miss** (no album named `name`), mint a fresh album named
     `name` (auto-suffixed per R5 if it collides) stamped with the fingerprint, then
     generate.
   - else `catalog.newest(style, vibe, fingerprint)` → the newest album matching the
     tags **and** the fingerprint, if any. A `(style, vibe)` hit with a *different*
     fingerprint is a miss (vox-1uo5), so it falls through to mint.
   - none → `catalog.mint_id()`, `store.create` a fresh `(style, vibe, name)` album
     stamped with `fingerprint`, `catalog.add`, then generate.
3. Seed a `Program` over the bound album's `ready_parts()`; post `SwitchProgram`.
   Full pool rotates from disk; partial resumes filling; empty generates — the
   unchanged activation classification.

### Test plan

Mirror source: one `test_*.py` per new module, extend the touched ones. Cover
happy + invalid + boundary + missing-dependency per PL-TT-3.

- **`album_id`** — `AlbumId.mint(taken)` avoids a taken set (collision loop lives
  here, finding #8); parse rejects non-hex/empty; equality/hash.
- **`album_tags`** — `TagQuery.matches` truth table (style-only, vibe-only, both,
  neither, name; finding #7); `slug()` for `(style,vibe)` and curated `name`; slug of
  names with spaces/slashes is filesystem-safe; wire round-trip.
  `mint_unique_name(desired, taken)` returns `desired` when free and `X1`/`X2` when
  taken (R5 auto-suffix).
- **`prompt_fingerprint`** — `from_prompts` is stable (same prompt-set → same
  fingerprint) and order-canonical; a changed base prompt or any changed variation
  yields a different fingerprint; parse rejects malformed hex; equality/hash (vox-1uo5).
- **`catalog`** — build from mixed manifests; `by_tags(query)` returns **many** albums
  sharing `(style,vibe)` newest-first; **`by_name` returns 0 or 1** (unique, R5);
  **`by_id(id)` is a direct one-album lookup, hit or miss, never routed through
  `TagQuery`/`select` (F#7)**;
  `newest` picks the latest tz-aware `created`; `newest(style, vibe, fingerprint)`
  matches only the same-fingerprint pool, so a `(style,vibe)` hit with a foreign
  fingerprint is a miss → `bind` mints fresh (vox-1uo5); `--name` resume binds by name
  even when the incoming fingerprint differs (named pool keeps playing); `add` makes a
  new album queryable;
  legacy (idless) dirs are excluded from every query; `bind`/`select` own resolution
  (finding #11) — `bind` resumes a named album or mints an auto-suffixed one on
  collision; `Album.locator` is opaque, never a `Path` (finding #3).
- **`selection` / `selection_playback`** — union spans two albums, each `SelectedPart`
  carries an opaque locator (no `pathlib` import in the module, finding #3); rotate
  never repeats with `#selection ≥ 2` and reuses `RotatePolicy` (finding #9);
  single-track replays; `wants_generation` is `False` (finding #1); empty selection
  is a caught boundary (no crash — mirrors the empty-pool guard).
- **`playback_source` / `control_signal` (lost race, finding #4)** — a fill outcome
  (`RecoveringFillOutcome`/transient) applied while the active source is a
  `SelectionPlayback` **rejects as `GuardViolationError` and mutates nothing** (no
  `AttributeError`); the writer survives (regression for the bas7-class crash). A
  `SwitchSelection` immediately after a posted `Produced` exercises the real race.
- **`manifest`** — round-trip `id`/`tags`/`created`/`prompt_fingerprint` (tz-aware UTC via
  `isoformat`/`fromisoformat`, finding #6; fingerprint round-trips as hex, vox-1uo5); `from_json` **raises** on a malformed or
  idless record (total, PY-EH-8, finding #5); `from_wire` peek surfaces `opt_str("id")`
  for the scan boundary; `ManifestDraft` carries no `created` (the store stamps it).
- **`filesystem_store`** — `scan` pairs dirs with manifests and **skips idless legacy
  via the `from_wire` peek** (finding #5); `create` takes a `ManifestDraft`, derives
  `<slug>-<id>`, **stamps `created = now(UTC)`** (store owns the clock, finding #6),
  keeps the path guard, and `mkdir(exist_ok=False)` regenerates on a directory race
  (finding #8); `open(directory)` accepts only scan/create-originated dirs
  (finding #10); a slug collision with a distinct id yields two live dirs; the store
  dereferences an opaque locator → `Path` (finding #3).
- **`service`** — `/music on` with a matching album **resumes** (no generation), with
  none **generates**; a `(style,vibe)` match whose stored fingerprint differs from the
  incoming prompt-set **mints a new album** rather than resuming (vox-1uo5), while a
  same-fingerprint match resumes; `--name X` resumes the unique album `X` even when its
  fingerprint differs (named pool keeps playing) or mints an auto-suffixed one (R5); `--name`/`style` override the tag; the recorded tag is the
  *session vibe*, not the style (move #1 regression test); replay of a two-album union
  plays cap-free with no fill; the service delegates resolution to `catalog.bind`/
  `catalog.select` and holds no `_bind_album`/`_tags_for` of its own (finding #11).
- **`fill_reconciler` / `loop`** — the reconciler reads `source.wants_generation`
  (finding #1): a `Program` in `RETRYING` (`filling=False`) **still keeps the fill
  running** (vox-ig52 stranded-retry regression), while a `SelectionPlayback` keeps it
  idle; the loop plays and rotates a `SelectionPlayback` identically to a `Program`
  (source-agnostic), resolving the dir via the `PlayerDirectory.locate` seam
  (finding #2). **The loop auto-advances a `SelectionPlayback` on a natural track-end
  identically to a `Program` (F#6):** the advance gate reads `source.is_playing`
  (`playing is not None` for a radio, `mode in {the playing modes}` for a program), so
  a radio does not stall on its first track — a regression test drives a two-track
  Selection through a track-end and asserts the loop posts the next `Rotate`.
- **wire/CLI/server** — `program_on` carries `vibe`; `program_select` resolves by
  style/vibe/name via a `TagQuery` **and by `id` via a direct `catalog.by_id` lookup
  (F#7 — `id` is not routed through `TagQuery`)**; `list` rows carry tags; `music` tool
  forwards `_session.vibe`.

### Z-model deltas for jms (`docs/audio-programs.tex`)

The active-Program transitions are **unchanged**. Album binding is a *resolution*,
not a transition — add one sentence to finding #7's neighbourhood: "the pool a
`TurnOn`/`VibeStyleChange` enters (`diskPool?`/`newPool?`) is resolved by a catalog
tag/name lookup before the operation runs, exactly as the 1-based part index is;
the model abstracts the lookup away." No schema change on `Program`. **The prompt
fingerprint (vox-1uo5) is part of that same resolution** — it selects *which* album a
`TurnOn` binds (same tags + same fingerprint resume, else mint), before the transition
— so it too is abstracted away: **no Z-model change.**

Add a **consume-only replay** section (per M1 — a no-generation rotation over a
Selection that may span albums):

1. A basic/derived set: `Selection : ℙ PART` (the model keeps `PART` opaque; the
   per-track directory is an implementation resolution, out of scope for Z, like
   the file token behind a `Part`).
2. A minimal `Radio` state schema: `selection : ℙ PART; playing, lastPlayed :
   optional PART`, predicate `playing ⊆ selection ∧ lastPlayed ⊆ selection` and
   `#playing ≤ 1 ∧ #lastPlayed ≤ 1`. **No `poolSize` bound** — this is the delta
   that makes union radio legal (`#selection` is unconstrained).
3. `StartRadio` (`selection? : ℙ PART`, `selection? ≠ ∅`): begins at a member,
   `playing' ∈ selection'`, `selection' = selection?`.
4. `RadioRotate`: the shape of `Rotate` with **generation framing removed** —
   `#selection ≥ 2 ⟹ playing' ∈ selection ∧ #playing' = 1 ∧ playing' ≠ playing`;
   `#selection = 1 ⟹ playing' = playing`; `lastPlayed' = playing`. No `pool`,
   `filling`, `attempts`, `failedParts`, `lastError` — a Radio has none.
5. `RadioNext ≙ RadioRotate` (as `Next ≙ Rotate`), and a Key Property: **"Replay
   generates nothing and is uncapped"** — `RadioRotate`/`StartRadio` touch no
   generation field and impose no `poolSize` bound, so a union of two full albums
   (24 parts) is a legal `Radio` state though never a legal `Program` state.
6. **A fill outcome applied while a `Radio` is active changes nothing
   (finding #4).** In the model this is `StaleFillIgnored` — an *enabled*, total
   `ΞRadio` no-op that accepts the stale outcome and mutates nothing, **not** a
   disabled operation. The only thing *disabled* is the successful `FillOk` branch,
   whose `filling = ztrue` guard has no inhabitant in a `Radio` (the same shape as
   `FillOk` being disabled outside `playing_filling`); the total `ΞRadio` no-op is
   what remains and covers the case, so it is never an error. This is the model image
   of the runtime lost-race idiom: the fill task posts `Produced` after a
   `SwitchSelection` retargeted the writer to a `SelectionPlayback`, the
   `isinstance(source, Program)` narrow fails, and the writer **rejects via
   `GuardViolationError`** and survives (logged at INFO) — a raise at the code level,
   a `ΞRadio` no-op in the model. Key Property: **"a stale generate outcome against a Radio never mutates
   and never crashes."**
7. **Leaving a `Radio` — `RadioOff` (F#2).** A `TurnOff` while a `Radio` is active is a
   valid stop, the parallel of `TurnOff` on a `Program`: `Radio → off/empty`, tearing
   the source down (`selection' = ∅`, `playing' = lastPlayed' = nothing`), *not* a
   rejected lost race. Model it as a total operation on the `Radio` schema.
8. **Source-agnostic retarget (F#2).** `StartProgram`/`SwitchProgram` may fire while a
   `Radio` is active, transitioning `Radio → Program`: the channel's active source is a
   union of `Program` and `Radio`, and a generate-mode start/switch replaces whichever
   source is current, regardless of shape — so `SwitchProgram` *from* a `Radio` is a
   legal retarget (user intent), just as `SwitchSelection`/`StartRadio` *from* a
   `Program` is. `Next` on a `Radio` is `RadioNext ≙ RadioRotate` (item 5). A
   `VibeStyleChange` while a `Radio` is active is a `ΞRadio` no-op (replay carries no
   vibe-switch) — modelled like the fill-outcome no-op (item 6), a guard-disabled
   transition rather than an error. Key Property: **"the user-intent signals (off, on,
   switch, next) act on either source; only generate-internal outcomes and auto
   vibe-switches are disabled against a `Radio`."**

`fuzz -t` must stay exit 0; recommend `/z-spec:test` (probcli) explores five
properties over the new schema: "no immediate repeat" and "playing ∈ selection" on
`RadioRotate`, the **guard-disabled no-op** of a fill outcome against an active `Radio`
(finding #4) — the bas7-class crash a transition-level model catches at design time —
and the two F#2 retargets: `RadioOff` (Radio → off) and `SwitchProgram` from an active
`Radio` (Radio → Program), so the user-intent stop/switch paths are covered alongside
the disabled generate-internal ones.

### Open risks / questions — resolved

- **R1 (layering) — ACCEPTED.** The `PlaybackSource` Protocol generalises the
  `ControlChannel`/`ProgramLoop` from `Program` to a source union. It is the only
  decomposition that honours *both* locks (no 12-cap on replay, Phase-1 unchanged)
  without duplicating the loop; the rejected alternative (a second parallel loop) is
  worse — two tasks racing one player. Both reviewers concurred. Findings #1, #2, #4
  narrow the Protocol and harden the concurrency core against the lost race.
- **R2 (CLI direct disk read) — CONFIRMED in scope.** `cli_music.py` stops
  constructing `FilesystemProgramStore` client-side; `list`/part-resolution become
  gateway calls. The catalog is daemon-side and authoritative.
- **R3 (legacy visibility) — CONFIRMED default.** Legacy idless dirs are invisible to
  vox (excluded from `list`, skipped at the `scan()` boundary), consistent with
  "deletable by hand, Finder-only."
- **R4 (`created` clock) — CONFIRMED, hardened by finding #6.** The store owns the
  clock (`datetime.now(UTC)`), the value is tz-aware end-to-end, and `newest` ties
  break by id. The store takes a `ManifestDraft` and stamps `created`.
- **R5 (`name` uniqueness) — RULED by operator (2026-07-08): names ARE unique.** The
  design's original recommendation (name as a non-unique tag) is **overturned**. No
  two albums share a `name`; `by_name(name)` returns 0 or 1; uniqueness is guaranteed
  at creation by auto-suffixing (`AlbumTags.mint_unique_name`). `(style, vibe)` remain
  freely non-unique — the arbitrary-albums lock is about the tag axes, not the name
  axis. `--name X` resolves to the unique album named `X` (resume if it exists); the
  auto-suffix applies only when a NEW album is minted whose desired name collides.
  Folded into the identity bullets, the manifest note, and the `catalog`/`album_tags`
  rows above.

### Design-review resolutions

Two label schemes are used throughout this document: **`finding #N`** refers to the
numbered gvr + rej design-review findings (1--13) resolved in the table below;
**`F#N`** refers to the subsequent Cursor/Copilot PR-review-round findings on the
design PR. Both are cross-references, not TODOs.

The gvr and rej reviews converged; the operator ruled R5. Each finding's disposition:

| # | Finding | Disposition |
|---|---|---|
| 1 | `PlaybackSource.filling` → `wants_generation` | **Applied.** `Program.wants_generation` returns `state.filling or mode is RETRYING` (preserves the vox-ig52 clause verbatim); `SelectionPlayback` returns `False`; `fill_reconciler.reconcile(source)` reads `source.wants_generation`. |
| 2 | Remove `locate` from `PlaybackSource`; keep it on the `active_context`/`PlayerDirectory` seam | **Applied.** Protocol narrowed to `{playing, rotate, wants_generation, is_playing}` (`is_playing` added per F#6 — the source-agnostic advance gate); `PlayerDirectory.active_directory()` → `locate(part) -> Path`; `Program` stays `pathlib`-free. |
| 3 | Domain stays `Path`-free — opaque locator, not a live `Path` | **Applied.** `SelectedPart` and `Album` carry an opaque locator (`AlbumId` or dir-name string, the `Part.identity` discipline); the store dereferences locator → `Path`. `selection.py`/`catalog.py` never import `pathlib`. |
| 4 | Lost-race crash (bas7-class) — fill outcome vs. active Selection | **Applied.** `ControlSignal.apply(source, /)`; generate-family signals narrow `isinstance(source, Program)` and reject via `GuardViolationError` (INFO-logged, writer survives) when the source is a `SelectionPlayback`. Widened `RecoveringFillOutcome.origin`/`apply` to `PlaybackSource`. Modelled in the Z-delta (item 6) and the test plan. |
| 5 | `from_json` total; scan boundary skips legacy | **Applied.** `from_json` requires `id`/`tags`/`created` and raises (PY-EH-8); added `ProgramManifest.from_wire`; `scan()` peeks `opt_str("id")` and skips idless dirs. Struck the "raises only when the caller demands an album" clause. |
| 6 | `created` tz-aware UTC; store owns the clock; `create` takes a draft | **Applied.** tz-aware `isoformat`/`fromisoformat` round-trip; store stamps `datetime.now(UTC)`; added `ManifestDraft(id, tags, parts)` so `create` produces the manifest-with-`created`. |
| 7 | `TagQuery` value type | **Applied.** `TagQuery(style?, vibe?, name?)` owns `matches(tags)`; `by_tags`/`newest`/`by_name` take or derive it, killing the repeated `(str\|None, str\|None)` tuple (PY-OO-7). |
| 8 | One mint algorithm | **Applied.** `AlbumId.mint(taken)` owns the collision loop; `Catalog.mint_id()` delegates; `mkdir(exist_ok=False)` is the second-line race guard. |
| 9 | Reuse `RotatePolicy` for no-immediate-repeat | **Applied.** `SelectionPlayback` injects the same `PlaybackPolicy.next_part` (`RotatePolicy`) that backs `Program.rotate`; the rule is defined once (matches `RadioRotate`). |
| 10 | State the `open(directory)` guard invariant | **Applied.** Documented on `store.py`/`filesystem_store.py` and in the invariants: `open` only ever receives `scan()`/`create()`-originated dirs under root; no wire/CLI path constructs a raw dir. |
| 11 | Push tag-resolution onto `Catalog`; service is an orchestrator | **Applied.** `catalog.bind(request) -> Album` and `catalog.select(query) -> Selection` own resolution; `service.turn_on`/`replay` seed a source and post a signal, holding no `_bind_album`/`_tags_for`. |
| 12 | Pin the vocabulary | **Applied.** See the vocabulary note below. |
| 13 | Verb parity | **Adjusted.** Adopted `SwitchProgram`/`SwitchSelection` (shared `Switch*` verb) rather than the two listed pairs — it achieves parity while leaving the existing `SwitchProgram` name untouched (minimal churn). |
| R5 | Name uniqueness (operator ruling) | **Applied.** Names unique (enforced), auto-suffixed at mint; `by_name` returns 0 or 1; `(style, vibe)` non-unique; line-27 prose amended in place. |

**Vocabulary (finding #12).** `Album` = the at-rest catalog binding (manifest +
opaque locator); `Program` = the live playback entity seeded from an `Album`;
`ProgramManifest` = the persisted record; `ManifestDraft` = the pre-`created` record
the store stamps. The user says "album" for all three.

**Not-applied / adjusted:** none rejected outright. Finding #13 is the only
adjustment — the class-name pair differs from the two the reviewer offered, for the
churn reason above. If the operator prefers `StartProgram`/`StartSelection` or
`SwitchToProgram`/`SwitchToSelection`, either is a trivial substitution before
dispatch; the design is otherwise verb-neutral.
