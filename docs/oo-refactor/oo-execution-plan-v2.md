# OO Execution Plan v2 — Post-Decomposition

Date: 2026-05-14
Based on: `oo-design-assessment-v2.md` + `music-package-design.md`

## Overview

Steps 0–17 decomposed the monoliths (`voxd.py`, `service.py`). This plan
addresses the 11 remaining design problems identified in the v2 assessment,
ordered by impact on both design quality and metrics. The music package
design (written with the other agent) is incorporated as Phase B.

## Phase A: Cross-cutting foundation (Steps 18–19)

These create types used by multiple subsequent steps.

### Step 18: Create `SynthesisSpec` dataclass

**Priority 1 from assessment.** The highest-impact single change.

**Create:** `src/punt_vox/types_synthesis.py`

```python
@dataclass(frozen=True, slots=True)
class SynthesisSpec:
    voice: str | None = None
    language: str | None = None
    rate: int | None = None
    provider: str | None = None
    model: str | None = None
    stability: float | None = None
    similarity: float | None = None
    style: float | None = None
    speaker_boost: bool | None = None
    api_key: str | None = None
    vibe_tags: str | None = None
    once: bool = False

    def validate(self) -> None: ...
    def to_client_kwargs(self) -> dict[str, object]: ...
```

**Update callsites (4 files):**
1. `__main__.py`: `unmute` and `record` build `SynthesisSpec` from CLI args
2. `server.py`: `unmute` and `record` tools build `SynthesisSpec` from session + params
3. `voxd/synthesis.py`: `synthesize_to_file(spec: SynthesisSpec, ...)` — 14 params → 2
4. `voxd/synthesis.py`: `try_direct_play(spec: SynthesisSpec, ...)` — 13 params → 2

**Replaces:** `_validate_voice_settings` (duplicated in __main__.py and server.py)

**Metrics improved:** avg_params (5.75→~2), method_ratio (new class)

**Verification:** `make check`

### Step 19: Create `ConfigStore` class

**Priority 3 from assessment.**

**Refactor in-place:** `src/punt_vox/config.py`

```python
class ConfigStore:
    def `__new__`(cls, config_dir: Path | None = None) -> Self:
        self = super().`__new__`(cls)
        self._dir = config_dir or DEFAULT_CONFIG_DIR
        self._durable_path = self._dir / "vox.md"
        self._ephemeral_path = self._dir / "vox.local.md"
        return self

    def read(self) -> VoxConfig: ...
    def read_field(self, field: str) -> str | None: ...
    def write_field(self, key: str, value: str) -> None: ...
    def write_fields(self, updates: dict[str, str]) -> None: ...
```

**Replaces:** 6 top-level functions + repeated `config_dir or DEFAULT_CONFIG_DIR`

**Metrics improved:** method_ratio (0.0→~0.8), class_to_func_ratio (0.08→~0.5)

**Verification:** `make check`

---

## Phase B: Music package (Steps 20–28)

Per `music-package-design.md`. The other agent is already working on parts
of this. Steps are:

1. Step 20: Create `voxd/music/` package with ``__init__`.py` and `types.py`
2. Step 21: Move TrackGenerator → `voxd/music/generator.py`, add `find_track()`
3. Step 22: Add domain methods to MusicScheduler, move to `voxd/music/scheduler.py`
4. Step 23: Extract Method on `loop()` (CC reduction)
5. Steps 24–29: One handler file + test per handler (6 pairs), remove writable setters
6. Step 30: Update daemon.py wiring, delete old files

**Metrics after:** All music modules ≤300 lines, ≤3 classes, max_complexity ≤8

---

## Phase C: Server dedup (Steps 31–32)

### Step 31: Extract shared `unmute`/`record` logic in `server.py`

**Priority 2 from assessment.** Worst duplication in the codebase.

After `SynthesisSpec` exists (Step 18), extract:
- `_synthesize_segments(client, segments, spec) -> list[dict]`
- `_record_segments(client, segments, spec, output_dir) -> list[dict]`

Both `unmute` and `record` become ~20-line wrappers. Reduces server.py by
~100 lines, max_complexity from 36 down significantly.

### Step 32: `SessionConfig` encapsulation

Add `_` prefix to all 11 public attributes. Add `@property` getters. Add
`set_notify(mode)`, `set_vibe(mood, tags)` with validation. This is a
mechanical change with many callsite updates but no design risk.

**Metrics improved:** public_attr_violations, encapsulation_ratio

---

## Phase D: Playback and synthesis cleanup (Steps 33–34)

### Step 33: Decompose `PlaybackQueue.play_audio`

**Priority 4 from assessment.** 155-line God Method.

Extract Method: `_validate_file`, `_compute_timeout`, `_spawn_and_wait`,
`_log_result`. All stay on `PlaybackQueue`.

**Metrics improved:** max_complexity (13→~5), avg_complexity

### Step 34: Extract `_api_key_context` in synthesis.py

Context manager for env-var save/inject/restore. Replaces duplicated
old_key/env_key_name logic in `synthesize_to_file` and
`_run_play_directly_sync`.

---

## Phase E: __main__.py decomposition (Steps 35–39)

### Step 35: `OutputFormatter` Strategy

Replace `_json_output`/`_quiet_output` globals with a Strategy class.
Single `emit(payload, text)` method.

### Step 36: `ApiKeyResolver` class

Encapsulate four-source mutual-exclusion key resolution. One class,
one public method: `resolve() -> str | None`. Removes 115 lines.

### Step 37: `AudioMigration` class

117-line function → class with `scan()`, `preview()`, `execute()`.

### Step 38: `DaemonRestarter` class

166-line restart sequence → class with `stop()`, `wait_port_free()`,
`start()`, `verify_health()`.

### Step 39: CLI command grouping

Remaining CLI functions organized into logical groups. This is the
lowest-priority step — it improves method_ratio but the CLI is naturally
procedural (typer forces functions).

---

## Phase F: Provider cleanup (Steps 40–41)

### Step 40: Move voice caches onto provider instances

`ElevenLabsProvider._voices` and `PollyProvider._voices` instead of
module-level `VOICES` dicts. Eliminates global mutable state.

**Metrics improved:** public_attr_violations (-7), encapsulation_ratio

### Step 41: Extract chunked synthesis helper

Shared chunking logic from ElevenLabs and OpenAI into a helper function
or mixin. Low priority — only 2 instances.

---

## Phase G: Remaining files (Steps 42–43)

### Step 42: `client.py` `__new__` conversion + split

Convert `VoxClient` and `VoxClientSync` from ``__init__`` to ``__new__``.
Consider splitting into `client_async.py` and `client_sync.py` if
module_size exceeds 300 after the conversion.

### Step 43: `watcher.py` chime extraction

Move chime resolution (~80 lines) to its own module. The watcher should
dispatch events; what happens with them is the consumer's business.

---

## Summary

| Phase | Steps | Files touched | Key metric impact |
|-------|-------|--------------|-------------------|
| A | 18–19 | types_synthesis.py, config.py, __main__.py, server.py, synthesis.py | avg_params, method_ratio |
| B | 20–30 | voxd/music/* (10 new files) | classes_per_module, max_complexity |
| C | 31–32 | server.py | max_complexity (36→~10), public_attr_violations |
| D | 33–34 | playback.py, synthesis.py | max_complexity |
| E | 35–39 | __main__.py (5 extractions) | module_size (1352→~500), method_ratio |
| F | 40–41 | providers/*.py | public_attr_violations |
| G | 42–43 | client.py, watcher.py | init_violations, module_size |

**Phases A and B are the priority.** A creates the cross-cutting types that
unblock C and D. B is already in progress with the other agent. C addresses
the worst complexity hotspot (server.py max_complexity 36). D–G are
diminishing returns but still improve the scorecard.

## What to leave alone

- `core.py`: Well-designed. Free functions are correct.
- `applet.py`: Small, focused.
- `VoxClientSync` duplication: Provides type safety. `__getattr__` would hurt.
- `playback.py` free functions: Genuinely stateless platform utilities.
