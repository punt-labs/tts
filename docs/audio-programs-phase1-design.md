# Audio Programs — Phase 1 Design (playlist format)

**Status:** SHIPPED (Phase 1, 2026-07-07). Author: Raymond H (`rmh`), 2026-07-05.
See the **Shipped deviations** note below for where the code differs from this design.
**Branch:** `design/audio-programs-phase1`. **Contract:** `docs/audio-programs.tex`
(the verified Z model, ownership-free as of commit 9bb5307). **Concept:**
`docs/audio-programs-concept.md`.

This design reframes today's music as a first-class, persisted **Program**
model (playlist format only — no new ElevenLabs engines) and adds **playlist
replay from both CLI and MCP**. Every state, transition, and invariant traces to
a named schema in the `.tex`. Where this doc says "schema `X`", it means the Z
operation or state schema of that name in `docs/audio-programs.tex`.

> **Shipped deviations (2026-07-07).** The code diverges from this design in a few
> places, all captured in `docs/design-review-phase1.md`: (1) the *format-general*
> claim is partly aspirational — `Program.rotate` raises on the `COMPLETE` arm,
> there is no terminal `Mode`, and `Subject` is concretely `PlaylistSubject`, so
> podcast/audiobook (Phases 2–3) need those seams *built*, not merely supplied;
> (2) `subject.vibe` records the *style*, not the session vibe — vibe does not yet
> partition pools (direction B shipped; per-vibe pools are `vox-q7vh`, direction
> A); (3) the per-command `applied/rejected` wire field is unreachable in Phase 1
> (handlers ack at enqueue, before the writer applies). The `vox music migrate`
> path (§3) and the `~/Music/vox/programs/` layout were struck — pools save to
> `~/Music/vox/<name>/` with ID3 tags.
>
> **Direction update (2026-07-25).** The remaining seams above are scoped by the
> 2026-07-25 decisions in `docs/audio-programs-concept.md`: one input type for a
> format's authored content that the CLI and MCP both build (tidy music first,
> as the reference); finite formats terminate while music loops; and
> long-generation per-Part status. The `.tex` models the terminate and per-Part
> transitions at the Phase-2 design gate, before podcast implementation.

**Ownership is gone.** Per the operator ruling of 2026-07-05, `voxd`'s Program
state is machine-universal: any client — an MCP agent session or the CLI, from
any process — may issue any command against it. There is no `SESSION` type, no
owner, no `who?` input, and no claim-before-control step anywhere in this design.
The model is strictly simpler for it: **9 state variables, 16 invariants, one
ungated advance.** Because any client now drives `voxd`, any client must also be
able to *see* what `voxd` is doing — so `status` is promoted here to a
first-class, format-spanning surface (§5).

The design is deliberately whole: it names classes, private state, injected
seams, the manifest, the write-set, the test plan, and the OO debt paydown. It
sketches code where the shape is load-bearing, but writes no `.py`.

---

## 0. Reading guide — model → code map (one table)

| `.tex` schema / symbol | Phase-1 code home |
|---|---|
| `[PART]` basic type | `Part` value object (`programs/part.py`) — carries its intrinsic manifest index |
| `[REASON]` (non-empty) | `Reason` value object (validates non-empty) |
| `Format ::= playlist \| podcast \| audiobook` | `Format` enum (`programs/format.py`) |
| `Mode ::= off \| generatingFirst \| ...` | `Mode` enum (`programs/mode.py`) |
| `PartStatus ::= pending \| ... \| failedPart` | `PartStatus` enum (`programs/part.py`) |
| `poolSize : Format ⇸ ℕ₁`, `maxRetry` | `Format.pool_size` / `MAX_RETRY` const |
| `Program` state schema (S-predicate, 16 invariants) | `ProgramState` frozen value object — **all invariants validated in `__new__`** (`programs/state.py`) |
| `Init`, `RestartFromDisk` | `ProgramState.initial()` / `ProgramState.restored(...)` classmethods |
| Generation ops (§7): `TurnOn` … `TurnOff` | `Program` generation-path methods (`programs/program.py`) |
| Consume ops (§8): `Rotate`/`Loop`/`TrackEnds`/`Next`/`Skip`, `PlayPart`, `StartFromDisk` | `Program` consume-path methods + injected `PlaybackPolicy` |
| `filling` flag (orthogonal, finding #2) | `ProgramState._filling: bool`, decoupled from `Mode.playing_filling` |
| `failedParts : PART ⇸ REASON` (finding #5) | `ProgramState._failed_parts: FrozenParts` |
| `lastError` (finding #5) | `ProgramState._last_error: Reason \| None` |
| Part indexing `playlist:2` (finding #7) | `Part.index` (intrinsic) + `PartRef` resolution in the surface, **not** domain state |
| `status` view (findings #5, #12) | `ProgramStatus` value object (`programs/status.py`), rendered by CLI **and** MCP (§5) |
| the on-disk pool / manifest | `ProgramManifest` + `FilesystemProgramStore` (`programs/store.py`, `programs/manifest.py`) |
| per-format generation backend | `Producer` protocol + `MusicProducer` (`programs/producer.py`) |

---

## 1. Domain model

The domain is a new package, `src/punt_vox/voxd/programs/`, that replaces
`src/punt_vox/voxd/music/`. The rename is not cosmetic: the model is
format-general, and Phases 2–3 extend it, so the package name must be too. This
is forward integration (PY-RF-6) — the old `music/` package is deleted and every
caller wired to `programs/` in the same PRs; no shims, no re-exports.

### 1.1 The state value object — `ProgramState`

The single highest-value class. It is the Z state schema `Program` made
executable: a `frozen=True, slots=True` value object whose `__new__` validates
**every** invariant in the schema predicate (S1–S16) before returning. Because it
is immutable and self-validating, an illegal state cannot be represented — the
property tests construct states and assert the invariants hold, and every
transition produces a fresh `ProgramState` that re-checks them.

```python
# programs/state.py  (sketch — not final)
@final
class ProgramState:
    """One legal Program state. All 16 Z invariants hold by construction."""
    __slots__ = (
        "_format", "_pool", "_failed_parts", "_playing", "_last_played",
        "_mode", "_filling", "_attempts", "_last_error",
    )
    _format: Format
    _pool: frozenset[Part]              # set semantics, faithful to the Z `pool`
    _failed_parts: FrozenParts          # Part ⇸ Reason, disjoint from _pool
    _playing: Part | None               # "optional Part" = at-most-one
    _last_played: Part | None
    _mode: Mode
    _filling: bool
    _attempts: int
    _last_error: Reason | None          # None ⟺ "∅"; absence is the contract

    def __new__(cls, ...) -> Self:
        self = super().__new__(cls)
        # assign, then validate — one guard per S-invariant, e.g.:
        #   S1  len(pool) <= format.pool_size
        #   S5  failed_parts.keys() disjoint from pool
        #   S7  attempts >= 1  <=>  mode is retrying
        #   S8  filling  =>  mode in {generating_first, playing_filling}
        #   S9  #last_error == 1  =>  mode in {retrying, failed}
        #   S10 mode == failed => last_error is not None
        #   S11 mode == off => playing/filling/last_error/failed all empty
        #   ... (S12–S16 mode-shape implications)
        self._check_invariants()        # raises ValueError on any violation
        return self
```

The nine slots are exactly the nine state variables of the schema — the earlier
`_owner` slot and its two invariants (`filling ⇒ owner ≠ ∅`, the off-clears-owner
clause) are gone with ownership. `_last_error: Reason | None` — the `None` is the
documented `∅` of the model (`#lastError ≤ 1`), not a "gave up" sentinel
(PY-TS-14 justification inline). Same for `_playing` and `_last_played`. The
at-most-one sets of the Z model become `T | None`, which is the faithful, precise
encoding.

`FrozenParts` is a tiny frozen value object wrapping the `Part ⇸ Reason` finite
map (an immutable mapping with `with_failure(part, reason)` returning a new one)
— not a raw `dict`, per PY-OO-4. It lives beside `Part`.

**Value equality (PY-OP-2).** `ProgramState` defines `__eq__`/`__hash__` over its
nine fields (dataclass-generated or explicit) so that two states built by
different transition paths compare equal iff their fields agree. The property
tests rely on this: "applying `Rotate` twice returns to an equal state when
`#pool == 1`" is a value-equality assertion, not an identity one. All nine fields
are themselves hashable value objects, so `ProgramState` is hashable and usable
as a set/dict key in reachability tests.

**Typed successor builder — no `**changes: object` hole (MAJOR-3, PY-TS-14 /
PY-OO-4).** Transitions never mutate a `ProgramState`; they build a successor.
Rather than a `_evolve(self, **changes: object)` bag that defeats the type
checker, a private keyword-only builder names every field with its exact type and
a per-field `_Unset` sentinel for "carry forward":

```python
_UNSET: Final = _Unset()   # a private singleton, distinct from None

def _with(
    self,
    *,
    pool: frozenset[Part] | _Unset = _UNSET,
    failed_parts: FrozenParts | _Unset = _UNSET,
    playing: Part | None | _Unset = _UNSET,
    last_played: Part | None | _Unset = _UNSET,
    mode: Mode | _Unset = _UNSET,
    filling: bool | _Unset = _UNSET,
    attempts: int | _Unset = _UNSET,
    last_error: Reason | None | _Unset = _UNSET,
) -> ProgramState:
    """Return a re-validated successor with the named fields replaced."""
    return ProgramState(
        format=self._format,
        pool=self._pool if isinstance(pool, _Unset) else pool,
        ...  # one line per field
    )
```

Every argument carries the field's real type; mypy/pyright check each call site.
`None` is a legal value (clears an optional), so it cannot double as "unchanged" —
hence the distinct `_Unset` sentinel. `format` is never a parameter: it is
invariant across every transition (`format' = format` in all schemas).

**One activation helper, shared by six transitions (MINOR).** Six schemas —
`TurnOn`, `FirstTrackOk`, `FillOk`, `VibeStyleChange`, `StartFromDisk`,
`Recover` — pick `(mode, filling, playing)` from the *shape of the pool* by the
same rule: empty ⇒ `generating_first`; partial ⇒ `playing_filling`; full ⇒
`playing_rotating`, with `filling` and `playing` following. That rule lives in
exactly one place:

```python
@dataclass(frozen=True, slots=True)
class _Activation:
    mode: Mode
    filling: bool
    playing: Part | None

def _activation_for(fmt: Format, pool: frozenset[Part],
                    playing: Part | None) -> _Activation:
    """Classify a pool by size into its (mode, filling, playing) activation."""
```

Callers that keep the fill inactive (`StartFromDisk`, finding #2) or force it on
(`Recover`) override `filling` after classifying; the size→mode→playing spine is
never duplicated. This is the antidote to the six-way copy-paste the informal
concept invited.

`Init` and `RestartFromDisk` become classmethods `ProgramState.initial()` and
`ProgramState.restored(fmt, disk_pool)` — they map field-for-field to the Z
schemas.

### 1.2 The entity — `Program`

`Program` is the mutable domain entity that owns the *current* `ProgramState`
and exposes one method per Z operation. Each method: checks the operation
precondition (raising `ValueError` on violation — that is the Z guard), computes
the successor `ProgramState` (which re-validates), and stores it. Methods return
`None` (mutators, PY-OP-8) or a small result object where the surface needs one.

```python
# programs/program.py  (sketch)
class Program:
    __slots__ = ("_state", "_policy")
    _state: ProgramState
    _policy: PlaybackPolicy               # injected rotate-vs-sequential seam

    def __new__(cls, state: ProgramState, policy: PlaybackPolicy) -> Self: ...

    # --- generation path (§7) — no session, no ownership -----------------
    def turn_on(self) -> None: ...                          # TurnOn
    def first_track_ok(self, new: Part) -> None: ...        # FirstTrackOk
    def first_track_bad_prompt(self, bad: Part, reason: Reason) -> None: ...
    def first_track_transient(self, reason: Reason) -> None: ...
    def fill_ok(self, new: Part) -> None: ...               # FillOk
    def fill_bad_part(self, bad: Part, reason: Reason) -> None: ...   # finding #5
    def fill_transient(self, reason: Reason) -> None: ...
    def retry_fails(self, reason: Reason) -> None: ...
    def retry_exhausted(self, reason: Reason) -> None: ...
    def recover(self) -> None: ...
    def vibe_style_change(self, new_pool: frozenset[Part]) -> None: ...
    def turn_off(self) -> None: ...

    # --- consume path (§8) — no generation, no session -------------------
    def rotate(self) -> None: ...          # Rotate = TrackEnds = Loop = Next = Skip
    def play_part(self, target: Part) -> None: ...  # explicit index, no anti-repeat
    def start_from_disk(self, target: Part) -> None: ...  # CLI cold start

    # --- observation (§5: status is a first-class, format-spanning view) --
    @property
    def mode(self) -> Mode: ...
    @property
    def status(self) -> ProgramStatus: ...   # mode + now-playing + generation + failed_parts
    @property
    def pool(self) -> tuple[Part, ...]: ...   # ordered by Part.index (MAJOR-1)
```

**One ungated advance (CHANGE 1).** `skip`, `next`, `loop`, and the automatic
track-end advance are all the *same* transition — the Z model defines
`Skip ≙ Next ≙ Loop ≙ TrackEnds ≙ Rotate`. There is exactly one method,
`rotate()`, taking no session and gated only on `mode ∈ {playing_filling,
playing_rotating, retrying}` and `#pool ≥ 1`. An automatic advance and a
user-driven advance are indistinguishable; any client drives either. `rotate()`
delegates the *choice* of the next Part to the injected `PlaybackPolicy` (§1.4).
`play_part()` bypasses the policy because the user named the Part (finding #7 — no
anti-repeat).

**Stable Part numbering comes from the Part, not the pool (MAJOR-1).** Each
`Part` carries its intrinsic manifest index (`part.index`, 1-based, assigned when
the Part is recorded and never reused). `ProgramState._pool` stays a
`frozenset[Part]` — faithful to the Z `pool` set — but `Program.pool` returns the
parts **sorted by `part.index`**, so `playlist:2` resolves to the same audio
across daemon restarts and across the CLI/MCP/daemon processes. The ordering is
derived from data on the Part, not from set-iteration order (which is
non-deterministic) and not from a separate list the state would have to keep
consistent.

**Precondition = Z guard.** `rotate`, `play_part` are enabled in
`playing_filling ∨ playing_rotating ∨ retrying` (finding #3 — playback survives a
transient backoff). `play_part`'s guard `target ∈ pool` is exactly "the addressed
index resolved to a *ready* Part"; an out-of-range index never reaches here (§4).

Why `Program` is not a god class: every one of its ~16 methods touches exactly
one attribute, `_state` (LCOM ≈ 0 — perfectly cohesive). Its size is bounded
because the invariant logic lives in `ProgramState`, not here; each method is a
guard + a `_with`. Dropping the ownership methods (`skip`'s owner-gated variant,
any claim step) makes it smaller still. If `program.py` approaches the 300-line
limit, the natural split is generation-path vs consume-path into two collaborators
the entity holds — but the first cut keeps them together while the count is ~16
short methods.

### 1.3 The Store protocol + filesystem impl + in-memory fake (DI seam)

The operator's hard requirement: *all program/part persistence and disk I/O is
behind an injected Protocol, with a filesystem impl in production and an
in-memory fake for tests; zero direct filesystem access in the domain/loop.*
This generalizes today's `TrackStore`.

Two narrow protocols (PY-IC-9, PY-DP-11), in `programs/store.py`:

```python
@runtime_checkable
class ProgramStore(Protocol):
    """The set of persisted Programs. The only place the programs root is read."""
    def list_programs(self) -> tuple[ProgramManifest, ...]: ...
    def resolve(self, name: ProgramName) -> ProgramManifest | None: ...
    def open(self, name: ProgramName) -> PartStore: ...   # scoped to one Program
    def create(self, manifest: ProgramManifest) -> PartStore: ...

@runtime_checkable
class PartStore(Protocol):
    """One Program's Parts on disk. Replaces TrackStore, scoped to a directory."""
    def ready_parts(self) -> tuple[Part, ...]: ...    # ordered by index, ready only
    def next_index(self) -> int: ...                  # the index a new Part will take
    def write_target(self, index: int) -> Path: ...   # where a new Part audio lands
    def record(self, part: Part) -> None: ...         # append to manifest + fsync
    def manifest(self) -> ProgramManifest: ...
    def prepare(self) -> None: ...
```

- **Production:** `FilesystemProgramStore` (root = `~/Music/vox/`) and
  `FilesystemPartStore` (one program directory + its `manifest.json`). These are
  the *only* modules importing `pathlib`/`json` for program data. `resolve`
  returns `ProgramManifest | None` because absence-by-name is the documented
  contract (PY-EH-8 / PY-TS-14 — justified inline); `open` on a missing program
  raises. `ready_parts` returns Parts already carrying their manifest index, so
  the domain's `pool` numbering is disk-authoritative (MAJOR-1).
- **Fake:** `InMemoryProgramStore` / `InMemoryPartStore` in `tests/programs/
  conftest.py`, structural (no inheritance), holding dicts in memory. Generalizes
  today's `FakeTrackStore`. The domain and the fill loop touch only the protocol,
  so every domain and loop test runs filesystem-free.

`resolve`'s return of `| None` and `list_programs` reading a directory are the
`RestartFromDisk` seam: the store loads a saved pool of ready Parts, the daemon
holds the Program idle (`off`) until turned on, and the CLI plays from disk with
no generation.

### 1.4 `PlaybackPolicy` (strategy seam)

`PlaybackPolicy` is the format axis for ordering/advance (finding #1: the
loop-vs-stop choice lives in `Rotate`, not in a new mode). A single-method
strategy (PY-DP-11), `programs/playback_policy.py`:

```python
@dataclass(frozen=True, slots=True)
class Advance:
    part: Part

class _Complete:                     # end-of-list singleton
    ...
COMPLETE: Final = _Complete()

AdvanceResult = Advance | _Complete

class PlaybackPolicy(Protocol):
    def next_part(self, pool: tuple[Part, ...], playing: Part | None) -> AdvanceResult:
        """Return the next Part (`Advance`) or signal end-of-list (`COMPLETE`)."""
```

- `RotatePolicy` (Phase 1, playlist): shuffle, avoid immediate repeat when
  `#pool ≥ 2`, replay the sole Part when `#pool == 1` (the only intended
  looping). **`RotatePolicy` never returns `COMPLETE`** — a playlist has no end.
  This is today's `TrackPool.pick_next`, promoted to a strategy.
- Phase 2/3 add `SequentialPolicy` (podcast/audiobook: advance in index order,
  return `COMPLETE` after the last Part). Returning a *result* rather than a bare
  `Part` is what lets a finite format signal end-of-list with **no breaking
  change** to the shared Protocol (MAJOR-2): `Program.rotate()` already branches
  on `Advance` vs `COMPLETE` (in Phase 1 the `COMPLETE` arm is unreachable and
  asserted so; in Phase 2/3 it transitions playback to a stopped state). No edit
  to `Program`/`ProgramState`.

The `AdvanceResult` union is a discriminated type, not a `Part | None` hole — the
`_Complete` case is named, so a reader (and the type checker) sees "end of list"
rather than "maybe absent". This is the PY-EH-8-preferred encoding.

### 1.5 `Producer` (per-format generation backend)

`Producer` is the "make one Part" backend — the seam between the format-general
fill orchestration and the format-specific ElevenLabs call. A single-method
strategy (PY-DP-11), `programs/producer.py`:

```python
class Producer(Protocol):
    async def produce(self, spec: PartSpec, target: Path) -> Part:
        """Author-to-audio for one Part: generate into `target`, return a ready Part.

        Raises ProducerBadInput on a permanent error (bad_prompt/ToS/missing key)
        and ProducerTransient on a transient one (429/quota/5xx/timeout) so the
        fill loop can route to the right Program transition (findings #4/#5).
        """
```

- `MusicProducer` (Phase 1): wraps `ElevenLabsMusicProvider.generate_track`.
  Absorbs today's `TrackGenerator.generate`. **Folds in vox-y3om** — instead of
  the hard-wired `_MUSIC_DURATION_MS = 120_000`, it samples a realistic,
  slightly-randomized length per Part (a `LengthPolicy`, playlist range 90–210 s).
  This lands here because Phase 1 is already in the generation path.
- The two exception types make the model's three failure branches
  (`FillOk`/`FillBadPart`/`FillTransient`, `FirstTrackOk`/`BadPrompt`/`Transient`)
  a clean routing decision in the fill loop, not a string sniff.
- Phase 2/3 add `DialogueProducer` / `NarrationProducer`. `Program`, the store,
  the policy, and the fill loop are untouched.

### 1.6 The fill loop and the playback loop

Today's `PoolFiller` mixes two concerns: (a) the cancellable single-flight async
task machinery, and (b) the provider call. The provider call moves to `Producer`
(§1.5); the async machinery stays as `Filler` (`programs/filler.py`) — one
cancellable sequential task per active Program that drives a `Producer` and feeds
results into the Program's transitions (`fill_ok` / `fill_bad_part` /
`fill_transient`). The single-flight lock, `asyncio.shield`, and orphan-discard
logic carry over unchanged in behavior — and are pinned by a 1:1 behavioral test
snapshot before the old code is deleted (§3, §7 MAJOR-4).

`ProgramLoop` (`programs/loop.py`) replaces `MusicLoop`: it owns the player
subprocess, races it against the pending control signal, and on track-end calls
`program.rotate()` then plays `program.playing`. It never generates.

**Test seams for the loops (MINOR).** Two injected seams keep the loop tests
deterministic and fast without mocking internals:

- A `Sleeper` protocol (`async def sleep(self, seconds: float) -> None`) backs the
  transient-retry backoff. Production injects `asyncio.sleep`; tests inject a
  no-op so backoff is instant and the `retrying → recover` path runs in
  microseconds.
- The player subprocess is a *real* trivial process in tests (`true` for a clean
  exit, `sh -c 'exit 3'` for a non-zero exit), not a mock. This exercises the
  actual `asyncio.create_subprocess_exec` boundary and drives `_log_exit`'s
  error branch (non-zero exit → logged, playback advances) so the ≥ 90% loop
  target includes the exit-handling branches, not just the happy path. Mocking
  the subprocess would test the mock; a trivial real process tests the boundary.

### 1.7 The control channel

`ControlChannel` (`programs/control.py`) — today's `MusicControlChannel`, now
just the control channel: it carries the on/off lifecycle and the single pending
control signal the loop reads. **With ownership removed it holds no owner and no
`SessionId`** — the earlier `MusicControlChannel` tracked which session turned the
Program on; that is gone. See §8 for the one substantive change that remains here
(the debt paydown): the control signal stops being a bare string `Literal` and
becomes a typed `ControlSignal` command.

---

## 2. Persistence — on-disk layout and manifest

Today: every track is a flat file in `~/Music/vox/tracks/`, and the "pool" is
inferred from a filename prefix `<vibe>_<style>_<ts>_<n>.mp3` (vox-us4g: "it's a
naming pattern, not a list"). This design replaces the pattern with an explicit,
named directory + manifest.

```text
~/Music/vox/
  <program-name>/
    manifest.json
    001.mp3
    002.mp3
    ...
```

`<program-name>` is the addressable identity — the `--name`, else the style
(`ProgramService._name_for`) — and every track carries ID3 tags
(artist/album/title/genre/track) so pools drop straight into macOS Music.app and
Ubuntu players. CLI, MCP, and daemon all resolve the same entity by name.

`manifest.json` (the `ProgramManifest` value object serialized) — the minimal
schema the CLI needs to play/advance **without the daemon regenerating**:

```json
{
  "name": "ambient_techno",
  "format": "playlist",
  "subject": {"vibe": "ambient", "style": "techno"},
  "parts": [
    {"index": 1, "file": "001.mp3", "status": "ready",  "duration_ms": 132000},
    {"index": 2, "file": "002.mp3", "status": "ready",  "duration_ms": 158000},
    {"index": 3, "file": "003.mp3", "status": "failed", "reason": "bad_prompt: ..."}
  ]
}
```

- `parts[].index` is the **intrinsic, stable Part index** (MAJOR-1): assigned
  once at record time, never reused, and the sole basis for `playlist:2`
  addressing. `Part` deserializes with it; `Program.pool` sorts by it.
- `parts[].status` promotes the model's `PartStatus` to a stored field. In Phase 1
  only `ready` and `failed` are ever written (finding #9: atomic delivery hides
  `pending`/`generating`); the schema carries them now so Phases 2–3 add long-gen
  statuses without a manifest migration.
- `subject` is a **typed value object, not a bare `dict[str, Any]`** (MINOR). In
  Phase 1 it is `PlaylistSubject(vibe: str, style: str)`. Podcast/audiobook add
  `PodcastSubject`/`AudiobookSubject` (topic/brief/language/level per concept
  §Content dimensions); `Subject = PlaylistSubject | PodcastSubject |
  AudiobookSubject` is a tagged union keyed by `format`, so (de)serialization is
  total and typed — no `Any` reaches the domain.
- `duration_ms` is what vox-y3om produces (varied length) and what the status
  "part N of M" progress display and a future finite-format progress bar need.

Addressing is uniform across the three callers: **by name** (`ProgramName`) for
the Program, **by 1-based index** (`PartRef`) for a Part, resolved against the
manifest's `parts` ordered by `index`. The daemon holds the active `Program`; the
store is the shared source of truth all three read.

---

## 3. Forward integration — `voxd/music/` → `voxd/programs/`

Per PY-RF-6, callers are wired to the new path and the old path is deleted in the
same PRs. The code decomposition mapping:

| Today (`voxd/music/`) | Phase 1 (`voxd/programs/`) | Disposition |
|---|---|---|
| `TrackStore` / `FilesystemTrackStore` | `PartStore`/`ProgramStore` + fs impls | **generalized + split**, old deleted |
| `TrackPool.pick_next` | `RotatePolicy.next_part` (returns `AdvanceResult`) | **extracted to strategy** |
| `TrackPool.is_full`, len | folded into `ProgramState` pool invariants + `Format.pool_size` | **absorbed**, old deleted |
| `Playlist` (pool identity + fill + selection) | split: `Program`/`ProgramState` (state), `Filler` (fill), `RotatePolicy` (selection) | **decomposed**, old deleted |
| `PoolFiller` | `Filler` (async machinery) + `Producer` (provider call) | **split**, old deleted |
| `TrackGenerator.generate` | `MusicProducer.produce` (+ vox-y3om length) | **moved**, old deleted |
| `TrackGenerator` naming/slug/list | `PartStore` + manifest; naming becomes `NNN.mp3` by intrinsic index | **absorbed**, old deleted |
| `MusicScheduler` (29-method facade) | `Program` (transitions) + `ControlChannel` (on/off + signal) + `ProgramLoop`/player (proc) | **decomposed — this is the debt paydown (§8)** |
| `MusicLoop` | `ProgramLoop` | **renamed + simplified** |
| `MusicControlChannel` | `ControlChannel` (typed signal, no owner, §8) | **renamed + de-owned + hardened** |
| `MusicControl` string Literal | `ControlSignal` command type | **replaced (§8)** |
| `Music*Handler` (6 wire handlers) | `Program*Handler` (list/play/loop/next/on/off/vibe/status) | **renamed + extended** |
| `MusicResponse`, `MusicMode`, `MusicState` | `ProgramStatus` + `Mode` (§1, §5) | **replaced** |

**Behavioral test snapshot before deletion (MAJOR-4, the bas7 lesson).** The
single-flight `asyncio.shield`/orphan-discard machinery in `PoolFiller` and the
control-race supervision in `MusicLoop` are the highest-subtlety code in the
package — bas7 (#291) shipped a broken loop precisely because an advance
transition was never listened to. Coverage percentage is **not** behavioral
parity (PY-RF-2). Therefore the existing `tests/music/test_filler.py` and
`tests/music/test_loop.py` behaviors are **ported 1:1 (retargeted names, same
assertions) onto `Filler`/`ProgramLoop` BEFORE `tests/music/` is deleted** — the
old and new tests run side by side in the migration PR until the new ones assert
the identical single-flight, orphan-discard, and control-race behavior, and only
then is the old package removed. New behavior (ungated rotate, status wiring)
gets new tests on top; it does not replace the parity snapshot.

**On-disk migration: none ships (superseded 2026-07-07).** An earlier draft
specified a one-time `vox music migrate` command (flat `~/Music/vox/tracks/*.mp3`
→ `programs/<name>/`) plus a start-up hint on a detected legacy dir. That was
**struck** under the org no-migration rule: Punt Labs products have no installed
user base, so a migration bridge is complexity for zero reason. There is no `vox
music migrate` command, no legacy-`tracks/` detection, and no start-up hint. Pools
are generated fresh and saved at `~/Music/vox/<name>/` — no intervening
`programs/` segment — with ID3 tags on every track.

---

## 4. CLI + MCP replay surface

Consume-only on the CLI (no LLM, free playback); create+consume on MCP. **No
command takes or checks a session** — any client drives any command (CHANGE 1).

### CLI (`vox music ...`, consume-only)

| Command | Maps to |
|---|---|
| `vox music list` | `ProgramStore.list_programs` |
| `vox music play <name>` | `StartFromDisk` (from `off`) → then auto-rotate |
| `vox music play <name> playlist:2` | resolve `PartRef(2)` → `PlayPart` |
| `vox music loop <name>` | `StartFromDisk` + `Rotate` on end |
| `vox music next` | `Rotate` (auto-advance is the same transition) |
| `vox music status` | read the active `Program.status` (§5) |

`playlist:N` part-addressing: the surface parses `<name> [format:index]`,
resolves the 1-based `index` against the manifest's `parts` ordered by intrinsic
`part.index` to a `Part` (MAJOR-1 — deterministic across restarts), then calls
`play_part`. **Out-of-range is a CLI resolution error reported before any
transition** (finding #7 — no "index out of range" transition exists in the
model). Exit non-zero, human message `"playlist has 3 parts; 5 is out of range"`.

There is **no claim step**: an agent (or the CLI) may `next`, `skip`, `loop`,
`vibe`, or `off` a Program regardless of which client started it. A CLI-started
Program is fully controllable from an MCP session and vice versa — that is the
whole point of removing ownership.

### MCP (`mic` tools, create + consume)

- Existing `music` (author + turn on), `music_play`, `music_list`, `music_next`
  keep working — `music` now *produces a Program* (manifest written) instead of
  loose files. Add `music_loop` and part-addressed play (`music_play` gains an
  optional `part` index). None of these tools takes a session/owner argument.
- The `status` tool is promoted to a first-class, format-spanning surface —
  specified in §5. It is the client-observability spine (vox-ig52), and it
  replaces log-reading for every client that now drives `voxd`.

---

## 5. Status — the format-spanning observability surface

Because any client now controls `voxd` (CHANGE 1), any client must be able to see
what `voxd` is doing. `status` is therefore a **first-class surface**, exposed
identically through the `mic` MCP `status` tool and the `vox`/`vox music status`
CLI. It reports the current Program **regardless of format** — music today,
podcast/audiobook later — from one value object whose shape does **not** assume
playlist.

### 5.1 The value object — `ProgramStatus`

```python
# programs/status.py  (sketch)
@dataclass(frozen=True, slots=True)
class NowPlaying:
    index: int              # 1-based position of the playing Part in the ordered pool
    of: int                 # M — total Parts currently in the pool ("part N of M")
    title: str | None       # optional display label from the manifest; never an address

@dataclass(frozen=True, slots=True)
class GenerationStatus:
    filling: bool           # a background fill is running
    attempts: int           # transient retries in flight (0 unless retrying)
    last_error: Reason | None   # program-level error (retrying/failed); None when healthy

@dataclass(frozen=True, slots=True)
class FailedPartView:
    index: int              # the intrinsic index of a Part that hit a permanent error
    reason: Reason

@dataclass(frozen=True, slots=True)
class ProgramStatus:
    name: ProgramName | None            # None ⟺ the daemon is idle (no active Program)
    format: Format                      # playlist | podcast | audiobook
    mode: Mode                          # off | generatingFirst | ... | failed
    now_playing: NowPlaying | None      # None when nothing is playing
    generation: GenerationStatus        # program-level generation/error surface (finding #5)
    failed_parts: tuple[FailedPartView, ...]   # per-Part surface (finding #5)

    @classmethod
    def idle(cls) -> ProgramStatus: ...   # no active Program
```

Every field is format-neutral. "Part N of M" (`now_playing.index` / `.of`) reads
the same for a playlist track, a podcast segment, or an audiobook chapter — the
status object carries **no** `vibe`/`style`/`topic` content, because that is
manifest *subject* data (§2), not runtime status. Phase 1 populates it from
playlist Parts; Phases 2–3 populate the same shape from dialogue/narration Parts
with **no field change**. That is what "spans formats now" means concretely: the
shape is decided here, once.

The two failure surfaces the model demands (finding #5) are both present and
distinct: `generation.last_error` is the *program-level* failure
(`retrying`/`failed`, nothing can play), and `failed_parts` is the *per-Part*
permanent failure while the Program plays on (`FillBadPart`). A design that
surfaced only one would silently drop the other — the invariant `#lastError = 1 ⇒
mode ∈ {retrying, failed}` is exactly why both must exist.

`Program.status` builds `ProgramStatus` from `ProgramState` plus the active
manifest's name/titles. `NowPlaying.index` is the playing Part's intrinsic index
(MAJOR-1); `of` is `len(pool)`.

### 5.2 Rendering and the format label

The surface label maps `Format.playlist → "music"` for humans (the CLI command is
`vox music`), while the wire/JSON carries the enum value `playlist`. `Format.label`
owns that one mapping so no caller hard-codes the string. The MCP tool returns the
serialized `ProgramStatus` as JSON; the CLI renders it as a short human block
(name, "music — playing 2 of 5", mode, any error, any failed parts).

### 5.3 Wiring `server.py` — the vox-73m5 hazard, named

`server.py` holds an in-memory `SessionState` dataclass. vox-73m5 shipped broken
because that session state cached music state that went stale — a vibe change was
reverted to a stale value the server still believed. **The status wiring must not
repeat that.** Concretely: the `status` tool (and the `vox music status` CLI)
reads the daemon's active `Program.status` **authoritatively, on each call**, via
`VoxClientSync` — `server.py` does **not** cache mode/now-playing/error in
`SessionState` and serve that copy. `SessionState` may keep MCP-session-scoped
data (e.g. the last vibe the *session* requested), but the *authoritative* Program
mode, now-playing, generation state, and failures come from `voxd` every time.
This is the boundary contract: a client asking "what is playing?" gets what the
daemon actually has, never a server-side shadow that can drift. A boundary test
drives the `status` tool through the server and asserts the JSON reflects a state
change made via a *different* path (a CLI `next`), proving there is no stale
server cache.

---

## 6. Full-scope generalization (Phases 2–3 fit without core rework)

The seams already carry the other two formats:

| Axis | Playlist (P1) | Podcast (P2) | Audiobook (P3) | Seam |
|---|---|---|---|---|
| Part backend | `MusicProducer` | `DialogueProducer` | `NarrationProducer` | `Producer` (§1.5) |
| Ordering/advance | `RotatePolicy` (never `COMPLETE`) | `SequentialPolicy` (`COMPLETE` at end) | `SequentialPolicy` | `PlaybackPolicy` (§1.4) |
| Pool size | `Format.pool_size(playlist)=12` | `=6` | `=6` | `Format` axdef |
| Manifest subject | `PlaylistSubject` | `PodcastSubject` | `AudiobookSubject` | `Subject` tagged union (§2) |
| Status shape | `ProgramStatus` | `ProgramStatus` (same) | `ProgramStatus` (same) | §5 — decided once |

`Program`, `ProgramState`, `ProgramStore`/`PartStore`, the loops, and the status
value object are format-general. What Phase 2/3 supply is a `Producer`, a
`PlaybackPolicy` (whose `COMPLETE` arm the shared `AdvanceResult` already
accommodates — MAJOR-2), a `pool_size`, and a `Subject` variant.

**One honest caveat (MINOR, model finding #9).** The six modes and the
`PartStatus` *vocabulary* are frozen now, and that is real: no new mode is added,
and the `AdvanceResult`/`Subject`/status seams absorb the format axes without
signature churn. But per finding #9, promoting `pending`/`generating` from the
single `filling` flag to **stored per-Part state** for the long-generating spoken
formats **is a state-signature change** — Phase 2/3 will add per-Part status
storage and the corresponding transitions. This design does not claim more
frozenness than the model grants: it freezes the *vocabulary* (so no manifest
migration and no new mode), while acknowledging the per-Part-status *storage*
expands. Pinning the modes and the free type now is what keeps that expansion
additive rather than a rewrite.

---

## 7. Test plan

Targets stated explicitly. Tests mirror source under `tests/programs/`.

### Coverage targets

- **Domain (pure, no I/O): ≥ 98% line + branch.** `ProgramState`, `Program`,
  `Part`/`FrozenParts`, `Format`, `Mode`, `RotatePolicy`, `Reason`,
  `ProgramManifest`, `ProgramStatus`. These are pure and cheap; near-total
  coverage is expected.
- **Store + Producer seams: ≥ 95%.** `FilesystemProgramStore`/`PartStore`
  (against `tmp_path`), `MusicProducer` (provider mocked at the boundary with
  valid MP3 bytes — `AudioSegment.silent`, per TESTING.md).
- **Loops (`Filler`, `ProgramLoop`): ≥ 90%**, driven against the in-memory fake
  with the injected `Sleeper` (instant backoff) and a trivial real subprocess.
  The 90% explicitly **includes `_log_exit`'s error branch** (non-zero player
  exit), reached by launching `sh -c 'exit 3'` (§1.6) — not just the clean-exit
  path.
- **Surfaces (CLI, MCP handlers): ≥ 90%**, including the boundary tests below.
- Overall new-package line coverage target: **≥ 95%** (`make coverage`).

### DI fakes

- `InMemoryProgramStore` / `InMemoryPartStore` — filesystem-free, in
  `tests/programs/conftest.py` (generalizes today's `FakeTrackStore`).
- `FakeProducer` — records `produce` calls; parametrized to succeed, raise
  `ProducerBadInput`, or raise `ProducerTransient`, so loop tests exercise all
  three model branches without a provider.
- `FakeSleeper` — no-op `sleep`, so retry backoff is instant.

### Migration parity snapshot (MAJOR-4) — runs FIRST

Before any `tests/music/` deletion: the `Filler` and `ProgramLoop` tests port the
`tests/music/test_filler.py` / `test_loop.py` behaviors 1:1 (retargeted names,
identical assertions) — single-flight `asyncio.shield`, orphan-discard on cancel,
control-race supervision, advance-on-track-end. Old and new run side by side in
the migration PR; the old package is deleted only once the new tests assert the
identical behavior. This is behavioral parity, not coverage parity (PY-RF-2).

### Property tests — invariants asserted **by name**

Each maps 1:1 to a "Key Property" in the `.tex` §9 (12 properties, 12 tests).
Parametrized over reachable states (built via the transitions), asserting after
every transition; they rely on `ProgramState` value equality (PY-OP-2):

1. `test_bounded_pool` — `len(pool) ≤ format.pool_size` always (playlist ≤ 12).
2. `test_generation_only_below_full` — `fill_ok` rejected at full; a full pool has
   `mode == playing_rotating ∧ filling is False`.
3. `test_at_most_one_fill` — `filling` ⇒ `mode ∈ {generating_first,
   playing_filling}`.
4. `test_playing_is_ready` — `playing is not None` ⇒ `playing ∈ pool`.
5. `test_failed_is_observable` — `mode == failed` ⇒ `last_error is not None`; the
   only two operations reaching `failed` set both.
6. `test_full_pool_never_hard_fails` — from any state with `#pool ≥ 1`, no
   transition sequence reaches `failed` (structural; assert `retry_exhausted`
   guard `pool == ∅`).
7. `test_two_failure_surfaces` — `fill_bad_part` records in `failed_parts` and
   leaves `last_error is None` and the Program healthy/playing (finding #5); both
   surfaces appear in `ProgramStatus`.
8. `test_replay_generates_nothing` — `play_part`, `rotate`, `start_from_disk`
   leave `pool` and `failed_parts` unchanged and never set `filling` (assert
   `FakeProducer.produce` never called).
9. `test_no_immediate_repeat` — `rotate` with `#pool ≥ 2` gives `playing' ≠
   playing`; `play_part` deliberately may repeat.
10. `test_retry_cap_empty_pool_only` — `retry_exhausted` requires `pool == ∅`; a
    non-empty pool recovers via `recover` while playback continues.
11. `test_off_clears_generation_state` — `mode == off` ⇒ `playing is None ∧
    filling is False ∧ last_error is None ∧ failed_parts empty` (no owner field
    exists to clear).
12. `test_no_session_gate` — no `Program`/`ProgramState` transition signature
    takes a session, and every advance/retune succeeds from any client;
    specifically `vibe_style_change` is reachable from `failed` unconditionally
    (finding #8), and `rotate` is enabled in `retrying` (finding #3).

### Boundary / failure tests

- **CLI part-addressing:** `playlist:2` plays index 2 deterministically across a
  simulated restart (MAJOR-1); `playlist:99` exits non-zero with an out-of-range
  message and **no** transition (finding #7); `playlist:0` and malformed
  `playlist:x` rejected at parse.
- **Empty pool:** `music next` / advance on `generating_first` is a no-op
  (finding #1); `play_part` on an empty pool raises (guard `target ∈ pool`).
- **Part failed:** `fill_bad_part` surfaces via `status`' `failed_parts`; program
  keeps playing.
- **Program failed:** `first_track_bad_prompt` → `mode == failed` + `last_error`
  surfaced via `status`; recovery via `vibe_style_change` from `failed` (no claim
  needed).
- **Missing key / provider down:** `MusicProducer` with no `ELEVENLABS_API_KEY`
  fails fast at turn-on (not a silent self-disable), asserted through the handler.

### Loop-level tests

Drive the real `ProgramLoop` + `Filler` against `InMemoryPartStore` +
`FakeProducer` + `FakeSleeper`: turn on an empty Program → first Part delivered →
plays → auto-rotate on track-end → fill to full → stops filling at `pool_size` →
rotates forever. And the resilience path: `FakeProducer` raising
`ProducerTransient` puts the Program in `retrying` while playback continues, then
`recover` re-arms the fill. This is the transition bas7 (#291) shipped broken —
advance-on-track-end — now covered by an executable test that listens to it.

### Status / MCP boundary test (client-observable, vox-73m5)

Call the `status` MCP tool through the server and assert the JSON carries `mode`,
`now_playing` ("N of M"), `generation.last_error`, and `failed_parts` — verifying
both failure surfaces reach a client, not just a log. Crucially, mutate the
Program through a *different* path (a CLI `next`) and assert the very next
`status` call reflects it — proving `server.py` serves the daemon's authoritative
`ProgramStatus`, not a stale `SessionState` cache (§5.3).

---

## 8. OO debt paydown target

**Target:** `MusicScheduler` (`src/punt_vox/voxd/music/scheduler.py`) — a
29-method facade with three disjoint method clusters, a PL-CO-2 violation
(">3 disjoint clusters = decompose") and a classic middle-man god class. It sits
right at 295 measured lines (the ratchet's 300 ceiling), so it is also the module
most at risk of tipping over on the next change.

The three clusters (disjoint instance-state → high LCOM):

1. **Session/lifecycle** — `turn_on`, `turn_off`, `update_vibe`, `skip_next`,
   `_adopt`, `_reset_session`, `disable` (touch `_channel`, `_state`). With
   ownership removed, the session-tracking half of this cluster (`_adopt`,
   `_reset_session`, owner bookkeeping) simply disappears — there is no session
   to track — leaving only the on/off/retune/advance lifecycle.
2. **Pool-selection pass-throughs** — `select_first`, `select_next_track`,
   `pool_empty`, `await_first_track`, `ensure_fill`, `mark_playing`,
   `mark_generating` (thin delegations to `_playlist`).
3. **Player-process lifecycle** — `begin_playback`, `kill_proc`, `proc`,
   `take_pending_track`, `has_pending_track` (touch `_proc`, `_pending_track`).

**Transformation (Extract Class + Split Module, PY-RF-3):** the migration
dissolves this facade into cohesive homes, each owning one cluster's state:

- cluster 1 → `Program` transitions (`turn_on`/`vibe_style_change`/`rotate`/
  `turn_off`) over `ProgramState`, plus `ControlChannel` for the on/off + control
  signal (no owner). The owner-adoption methods are deleted outright, not moved.
- cluster 2 → `ProgramState` pool + `RotatePolicy` (selection) + `Filler`
  (fill/first-track handshake). The pass-through methods vanish — callers use the
  owning object directly (no middle man).
- cluster 3 → the player-process lifecycle moves to `ProgramLoop` (which already
  owns the subprocess), removing the split-ownership of `_proc` between scheduler
  and loop.

**Bundled sub-transformation (Replace Primitive with Object):** the
`MusicControl = Literal["none","off","skip","play","vibe"]` string signal drives
a conditional forest across `MusicLoop._await_first`, `_supervise`, and
`_next_track` (loop `max_complexity == 8`, the highest in the music package). It
becomes a typed `ControlSignal` on `ControlChannel`, dispatched polymorphically,
collapsing those forests.

**Metrics improved (expected deltas):**

- **LCOM / responsibility-count (PL-CO-2):** `MusicScheduler`'s 3 disjoint
  clusters → cohesive single-responsibility classes each with LCOM ≈ 0. The
  middle-man (PY-OO-7) is eliminated: ~9 pure pass-through methods deleted, and
  the owner-tracking methods deleted rather than rehomed.
- **`max_complexity` (PL-OA-2):** loop 8 → ≤ 5 after `ControlSignal` dispatch.
- **`module_size`:** the 295-line `scheduler.py` is deleted; its responsibilities
  land in modules each well under the 300 ceiling, moving the whole area off the
  ratchet edge.
- Net: `check-oo` improves on every touched file; no metric regresses.

This is not padding bolted onto the feature — decomposing this god-facade **is**
how the migration lands cleanly. Ownership removal makes the paydown deeper: an
entire responsibility (session adoption) is deleted, not merely relocated.

---

## 9. Proposed write-set

The design owns the write-set (PY-IC-9/PY-OO-2 — extract new modules, don't cram).
Grouped for rollback-coherent PRs.

### Create — `src/punt_vox/voxd/programs/`

- `__init__.py` — package facade, `__all__`.
- `format.py` — `Format` enum + `pool_size` + `label`.
- `mode.py` — `Mode` enum.
- `identifiers.py` — `ProgramName`, `Reason` value objects (no `SessionId`).
- `part.py` — `Part` (with intrinsic `index`), `PartStatus`, `FrozenParts`,
  `PartRef`.
- `state.py` — `ProgramState` (all 16 invariants, `__eq__`/`__hash__`, `_with`,
  `_activation_for`) + `initial`/`restored`.
- `program.py` — `Program` entity (transitions).
- `status.py` — `ProgramStatus`, `NowPlaying`, `GenerationStatus`,
  `FailedPartView` (§5).
- `playback_policy.py` — `PlaybackPolicy` protocol, `Advance`/`COMPLETE`
  (`AdvanceResult`), `RotatePolicy`.
- `producer.py` — `Producer` protocol, `PartSpec`, `MusicProducer`,
  `LengthPolicy` (vox-y3om), `ProducerBadInput`/`ProducerTransient`.
- `manifest.py` — `ProgramManifest`, `Subject` union (`PlaylistSubject` …)
  (+ (de)serialize).
- `store.py` — `ProgramStore`/`PartStore` protocols + `FilesystemProgramStore`/
  `FilesystemPartStore`.
- `filler.py` — `Filler` (async single-flight fill driving a `Producer`).
- `control.py` — `ControlChannel` + `ControlSignal` (typed, no owner).
- `loop.py` — `ProgramLoop` + injected `Sleeper`.
- `playback_cmd.py` — moved from `music/` (player argv; unchanged behavior).
- `migrate.py` — one-time legacy `tracks/` → `programs/` migration, invoked by
  the explicit `vox music migrate` command (§3).
- handlers: `list_handler.py`, `play_handler.py`, `loop_handler.py`,
  `next_handler.py`, `on_handler.py`, `off_handler.py`, `vibe_handler.py`,
  `status_handler.py`.

### Create — `tests/programs/`

- `conftest.py` (in-memory fakes: `InMemoryProgramStore`/`PartStore`,
  `FakeProducer`, `FakeSleeper`, mock WS), plus one `test_*.py` per source module
  and `test_invariants.py` (the 12 named property tests), `test_loop_session.py`
  (loop-level), `test_migration_parity.py` (MAJOR-4 snapshot),
  `test_cli_programs.py`, `test_mcp_status.py` (status boundary + vox-73m5
  no-stale-cache).

### Modify

- `src/punt_vox/voxd/daemon.py` — wire `programs/` (store, program, producer,
  loop, handlers) in place of `music/`; log the migrate hint on a detected legacy
  dir (no auto-mutation).
- `src/punt_vox/server.py` — `status` tool returns the daemon's authoritative
  `ProgramStatus` (mode + now-playing + `last_error` + `failed_parts`), read per
  call, **not** cached in `SessionState` (§5.3); `music`/`music_play`/`music_list`/
  `music_next` retargeted; add `music_loop` + part index. No session/owner args.
- `src/punt_vox/__main__.py` — `music` CLI group: `list`/`play <name>
  [format:index]`/`loop`/`next`/`status`/`migrate`, consume-only.
- `src/punt_vox/client.py` — client methods for the new/renamed wire messages,
  including `status`.
- `src/punt_vox/voxd/__init__.py`, `voxd/daemon.py` imports — drop `music`
  re-exports, add `programs`.

### Delete (same PRs as their replacements — forward integration)

- entire `src/punt_vox/voxd/music/` package (17 modules).
- entire `tests/music/` (rewritten as `tests/programs/`) — **only after** the
  MAJOR-4 parity snapshot asserts identical `Filler`/`ProgramLoop` behavior.

---

## 10. Confirmed decisions and remaining open questions

**Confirmed by the operator (no longer open):**

- **No ownership.** `voxd` state is machine-universal; every command is
  session-free (CHANGE 1, model commit 9bb5307). This design carries no
  `SessionId`, `owner`, `who?`, or claim step anywhere.
- **R1 — explicit migration.** `vox music migrate` is an explicit command, not
  start-up auto-migration; the daemon only logs a hint (§3).
- **R2 — one active Program.** `ProgramStore` holds many saved Programs; the
  daemon animates one at a time; `play <name>` swaps which manifest backs the
  active Program. No two Programs play at once in Phase 1.
- **R3 — index Part addressing.** Parts are addressed by 1-based index within a
  named Program (`playlist:2`), resolved against the intrinsic `part.index`.
  Custom names live at the *Program* level; a today's `--name X` track becomes its
  own single-Part Program named `X`. The manifest keeps an optional per-Part
  `title` for display only, never as an address.

**Still open (surfaced or sharpened by the simplification):**

- **O1 — `status` when idle vs. when off with a saved pool.** With ownership
  gone, `status` is the only way a client learns what `voxd` holds. Two "not
  playing" situations differ: (a) no active Program at all (`ProgramStatus.idle`,
  `name is None`), and (b) an `off` Program whose pool is on disk, playable by
  `vox music play <name>`. Recommend `status` report (b) as `mode == off` with the
  `name` set and `now_playing is None`, so a client can tell "there is a pool to
  play" from "there is nothing". Confirm this is the wanted distinction, since it
  shapes what the daemon keeps resident while `off`.
- **O2 — concurrent commands, no owner to serialize them.** Removing ownership
  removes the accidental serialization it provided: two clients can now issue
  `next` and `vibe` against the same Program in the same instant. The Z model is a
  sequential state machine; the daemon must apply commands one at a time. Recommend
  the daemon process the control channel strictly serially (single-consumer
  `ControlChannel`), so "any client, any command" never means two transitions
  interleave. Flag for the implementation review — it is a daemon concurrency
  contract, not a domain one, but it is the one real hazard the ownership removal
  introduces.

Secondary (not blocking): `poolSize(playlist)=12` and `maxRetry=5` are the model
constants — confirm they stay config-fixed (not user-tunable) in Phase 1; the
vox-y3om length range (90–210 s) is a `LengthPolicy` default I picked — confirm
the range.
