# Audio Programs Phase 1 — Design Review (reference, not action)

**Captured:** 2026-07-07. **Status:** assessment only — nothing here was changed.
**Purpose:** judge whether the codebase is genuinely *well-designed*, not merely
*well-decomposed* by the OO ratchet. To be read before the Phase-2 design
dispatches and alongside the post-ship "ratchet situation" discussion.

**Sources:**

- `gvr` (Guido R.) — Python API design, protocols, typing, module structure.
- `rej` (Ralph Johnson) — design patterns, class responsibilities, framework design.
- `make report --threshold` — the absolute-OO-debt map (distinct from the
  relative ratchet gate `make check` enforces).

Both reviewers were read-only, ran independently, and were told to flag
ratchet *artifacts* specifically and to include a balanced "what's genuinely
well-designed" section.

---

## Executive synthesis

**The verdict both reviewers reach independently:** the `programs/` domain *core*
is genuinely well-designed — "the kind of work you extract *into* a framework,
not guess at" (rej). `ProgramState` as an executable Z schema, the single-writer
`ControlChannel` command bus, the `JsonObject` wire boundary, and
`PlaybackHealth` as a deliberate out-of-model surface are cited by both as
exemplary. The decomposition is real, not cosmetic.

**But the "well-decomposed ≠ well-designed" gap you suspected is real, and in the
most important place the *ratchet itself is the cause, not the author.*** Both
reviewers independently single out the handler/signal roster — 7–8 one-class,
five-line modules — as over-fragmentation forced by the `classes_per_module ≤ 3`
ceiling atomizing a naturally-cohesive Command family. gvr: *"not the author
gaming the metric; it is the metric fighting a Command/Handler pattern-family
that is idiomatically one module."* rej: *"the ratchet mis-serving the design,
worth a `--rebaseline` conversation rather than seven files."* This is direct
evidence for the ratchet-policy discussion.

**None of the findings block shipping Phase 1.** The feature works (verified by
the audio flight). Every finding is one of: Phase-2 readiness, a design-quality
refactor, a latent (non-active) wire inconsistency, or a ratchet-policy matter.

---

## Convergent findings (both reviewers, independently) — highest confidence

1. **The "format-general" thesis is oversold vs. the code (Phase-2 readiness).**
   The design claims Phase 2/3 drop in via a new `Producer`/`PlaybackPolicy`/
   `Subject` "with no edit to `Program`/`ProgramState`." The code contradicts it:
   `Program.rotate` raises `AssertionError` on the `COMPLETE` arm
   (`program.py:248`) instead of transitioning; there is **no terminal `Mode`**
   for a finite format that reaches its end; and `Subject` is hard-wired to
   `PlaylistSubject` (`manifest.py:109,185`, `active_context.py:46`,
   `filler.py:77`) — the promised `PlaylistSubject | PodcastSubject |
   AudiobookSubject` union does not exist. Only `Producer`, the store seam,
   `pool_size`, and `ProgramStatus` are actually format-neutral.
   *Both recommend:* reconcile the design doc to the shipped reality now, and
   (cheaply) introduce the `Subject` union + a real `Complete`/`Mode.ENDED`
   branch — modelled in the `.tex` — **before Phase 2 dispatches.**

2. **`SwitchProgram.apply` discards its `program` argument (design smell).**
   `ControlSignal.apply(program)` means "mutate this program" for eight signals
   and "throw it away and retarget the channel" for one (`switch_signal.py:51`,
   the tell is the `_program` rename). One protocol method, two meanings.
   *Both recommend:* either `apply()` (no arg) with each signal holding what it
   needs, or a distinct channel `post_switch()` seam.

3. **The O2 single-writer invariant is conventional, not structural.**
   `ControlChannel.retarget` (`control_channel.py:85`), `ActiveContext.switch`
   (`active_context.py:77`), and `Filler.ensure_running/cancel` are *public*
   mutators guarded only by docstrings. The mutation path *is* structurally
   safe; these *bypass* paths are not. rej: prefer making them private and
   letting the channel perform the swap itself — which **also resolves #2**
   (collapse the two findings). This is the deferred Fix #5a.

4. **`music_mode` is a session shadow that can drift from authoritative status.**
   The MCP `status` payload returns both `music_mode` (a session field, set in
   `music`/`music_play`/`music_next`) and the authoritative `program.mode`. A CLI
   `vox music off` leaves an MCP session's `music_mode` at `"on"` next to
   `program.mode = off` — the vox-73m5 drift the design's own §5.3 argues
   against, reincarnated in one payload (`server.py:897,900`).
   *Both recommend:* derive the human "music on/off" line from `program.mode`;
   delete `music_mode` from the session.

5. **`format` means two different things on two tools (latent wire break).**
   `program_list` emits `manifest.format.label` → `"music"`
   (`list_handler.py:44`); `program_status` emits `self.format.value` →
   `"playlist"` (`status.py:123`). Same program, two values, same field name.
   Fixing it later forces a breaking wire change on one tool.
   *Both recommend:* the wire always carries the enum value; the *label* is a
   CLI/human rendering concern only.

6. **Ratchet-driven over-fragmentation of the handler/signal roster.**
   Seven one-class handler modules (`off_handler.py`/`next_handler.py` are
   five-line bodies) + five one-class signal modules. Tracing one skip touches
   seven files. Both attribute this to the `classes_per_module ≤ 3` ceiling, not
   the author, and recommend a `handlers.py`/`signals.py` (or subpackage) group
   plus a ratchet-policy exemption for single-method Command/Strategy families.
   **This is the headline answer to "well-decomposed vs well-designed."**

7. **The applied/rejected (F7) surface is unreachable in Phase 1.**
   Handlers ack at *enqueue* (`command_handler.py:38`), before the writer applies
   the command; a `GuardViolationError` is logged at INFO and swallowed
   (`control_channel.py:159`). So the daemon never writes `applied: false`, and
   `CommandOutcome.rejected` / the `applied` field are dead paths. A `next` that
   loses a race to a concurrent `off` tells the client it succeeded — the exact
   "reading a log is not a strategy for a client" gap the design most loudly
   protects. (Known/deferred to slice 4 per the code comment.)
   *rej recommends:* until the writer can report back, either await application
   and report the real outcome, or drop `applied` from the Phase-1 wire so the
   surface does not promise a distinction the daemon cannot make.

---

## The ratchet's blind spot (the "well-decomposed ≠ well-designed" evidence)

Three independent data points, from both reviewers and `make report`:

- **`classes_per_module ≤ 3` atomized a cohesive Command family** into 7–8 files
  (finding #6). The metric forbids the more readable single `handlers.py`.
- **`method_ratio` / `class_to_func_ratio` penalize legitimately functional
  modules.** `make report` shows 28 files failing each — overwhelmingly
  procedural modules (`paths.py`, `dirs.py`, `keys.py`, `quips.py`, `mood.py`,
  `normalize.py`) that *should* be functions. Chasing those metrics would add
  unpythonic OO ceremony. gvr's principle: "don't reach for a class when a
  function will do."
- **The client `_SyncRunner`/`_VoxdTransport` splits** were partly LCOM-delta
  driven (documented in this session), not purely design-motivated.

The ratchet optimizes proxies (size, coupling, cohesion). It cannot see: are the
abstractions the right ones? do the seams match the domain? is an invariant
enforced by *structure* or by a *docstring*? Those are exactly findings #1–#7.

**For the ratchet discussion:** consider (a) a `classes_per_module` exemption for
single-method Command/Strategy/Handler families grouped in a subpackage; (b)
scoping `method_ratio`/`class_to_func_ratio` to modules that actually define
classes, so procedural modules aren't penalized; (c) the per-commit-vs-tip
scoring gap and the missing per-file reconciliation path noted separately.

---

## `make report --threshold` — absolute OO-debt map

`make check` (the CI gate) is the *relative* ratchet and is green. `make report`
is the *absolute* target view, and it fails widely — expected, since the codebase
"does not yet fully comply" (adopted 2026-05-13). Genuine debt it correctly flags:

- **God-modules by size (target ≤300):** `__main__.py` **861**, `normalize.py`
  **654**, `hooks.py` **459**, `client.py` 405, `doctor.py` 347,
  `providers/elevenlabs.py` 305.
- **Complexity (target ≤10):** a function at CC **20**; `normalize.py`/
  `espeak.py`/`resolve.py`/`__main__.py` at 11–12.
- **2 `init_violations`** (`__init__` as constructor where the standard wants
  `__new__`).
- **28× `method_ratio` + 28× `class_to_func_ratio`** — mostly the metric being
  wrong for functional modules (see above), not real debt.

---

## What is genuinely well-designed (both reviewers, verbatim highlights)

- **`ProgramState` + `StateInvariants`** — executable Z schema; illegal states
  cannot be constructed; typed successor builder with `_Unset` sentinel.
- **`ControlChannel`** — single-writer bus; the `finally`-always-`changed`
  discipline; the `GuardViolationError` (benign lost race, INFO) vs `ValueError`
  (corrupt successor, ERROR) distinction — an honest concurrency contract, not a
  defensive `except Exception`.
- **`JsonObject` (`wire.py`)** — the disciplined deserialization boundary;
  raising accessors, `object` not `Any`, coercion on a value object. The
  reference implementation of the codebase's own standards.
- **`Producer` + typed `ProducerBadInputError`/`ProducerTransientError`** — the
  three failure branches become a routing decision, not a string-sniff.
- **`PlaybackHealth`** — recognizing a player-spawn failure is *not* a Z
  transition, so it lives outside the state machine yet still crosses the wire
  via `ProgramStatus`. A sophisticated modelling call.
- **`ProgramStatus`** — genuinely format-neutral ("Part N of M"): the one
  extension axis the design got fully right.
- **`ProgramGateway` + `ClientProgramGateway` + fake** — humble-object
  testability across the wire.
- **The client transport split** — `_VoxdTransport` (framing) vs the RPC surface;
  `_SyncRunner` (event loop) vs the sync facade.

---

## gvr-specific findings (API / typing)

- **Wire-contract types live under `voxd/` but are imported by CLI, MCP server,
  and both gateways** (`cli_music.py:24`, `program_gateway.py:17`,
  `client_gateway.py:17`, `server.py:34`). No runtime coupling (pure types), but
  the dependency arrow points presentation → daemon-internal package (PY-IC-8 in
  spirit). Relocate the dual-consumer contract to a neutral `punt_vox/programs/`.
- **MCP tool surface incomplete + inconsistently shaped.** No `music_loop` tool;
  `music_play` hardcodes `part=None` (`server.py:696`) though the whole stack
  supports part-addressed play; `music` overloads on/off as a `mode: str` arg
  while `next`/`play`/`list` are separate tools — two shapes for one verb family.
- **`_SyncRunner.run(coro: Any) -> Any` forces ten `# type: ignore`** in
  `client_sync.py`. A PEP 695 generic `def run[T](self, coro: Coroutine[…, T]) ->
  T` removes all ten mechanically — the single cheapest typing-debt retirement.
- **Fat `TTSProvider` protocol** (`types.py:124`) — nine members across
  synthesis, voice resolution, language inference, health (PL-CO-2). Segregate,
  the way `DirectPlayProvider` already models an optional capability.
- **`StartRequest` should be `kw_only=True`** — three fields, first two
  `str | None`, positionally transposable (`program_control.py:30`).
- **`dict[str, Any]` at the client boundary** (`client.py:211`) should be
  `dict[str, object]` to match the domain's own discipline.
- **`types.py` nits:** inconsistent enum casing (`AudioProviderId` lowercase vs
  `Mode`/`Format` UPPER_SNAKE, PY-CS-1); module-level aliases
  (`SynthesisRequest = AudioRequest`, `result_to_dict = …`) — compat shims to
  delete (PL-PP-1).

## rej-specific findings (patterns / responsibilities)

- **`PlaybackStatus` enum has zero consumers** (`mode.py:11`) — a whole enum +
  `_STATUS` mapping + two properties for a collapse no surface performs. Delete.
- **The retry-machine predicate is split** between `TransientFailure.apply`
  (`fill_signal.py:70`, an `if/elif` mode forest) and `Program.retry_exhausted`'s
  guard — feature envy + a duplicated predicate that can drift. Give `Program`
  one `retry(reason)` that owns the exhaustion rule.
- **`ProgramService` static helpers are feature-envious** (`_final_prompts`,
  `_subject_for`, `_name_for`, `_target_in`) — push each onto the value object
  whose vocabulary it uses (PY-OO-7).
- **`FillReconciler` is a borderline thin mediator** (two-line `if`) — leave it,
  but fold back into the channel if it stays trivial through Phase 2.
- **`_PLAYING_MODES` duplicated** in `program.py:25` and `loop.py:41` — hoist to
  `mode.py`.
- **CLI reads the filesystem store directly for `list`/part resolution**
  (`cli_music.py:82,151`), bypassing the `ProgramGateway` the other verbs use.
- **Design-doc staleness:** §3 migration is correctly *absent* from code (struck
  per the no-migration gate) but still described in the doc; the programs-root
  also diverges (`~/Music/vox/` not `~/Music/vox/programs/`). Reconcile the doc.

---

## Recommended sequencing (NOT now — reference for later)

- **Before the Phase-2 design dispatches (cheap now, expensive later):**
  reconcile the framework claims (#1) and decide the applied/rejected surface
  (#3). rej: "the single most valuable next move… both are cheap to fix in the
  design now and expensive to discover in implementation."
- **Post-ship ratchet discussion:** the `classes_per_module` exemption for
  Command/Strategy families (#6); `method_ratio`/`class_to_func_ratio` on
  functional modules; the per-commit-vs-tip scoring gap; the missing per-file
  reconciliation path.
- **Optional Phase-1 follow-ups (low risk):** `music_mode` derive-from-status
  (#4); `format` wire consistency (#5); the deferred Fix #5a/b/c (O2 structural,
  SwitchProgram protocol, `_WIRE_TYPE` enforcement); the `_SyncRunner` generic.

Full agent transcripts are preserved in the session task outputs.
