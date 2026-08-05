# OO Design Assessment v2 — Post-Decomposition

Assessed by rej (Ralph Johnson) on 2026-05-14. Covers the 15 remaining
problem files after Steps 0–17 of the refactoring plan completed.

## Prioritized Action List

### Priority 1: `SynthesisSpec` dataclass

**Files:** `__main__.py`, `server.py`, `client.py`, `voxd/synthesis.py`

The 10–14 parameter synthesis signature appears in 6 locations: `unmute`,
`record`, `server.py:unmute`, `server.py:record`, `synthesis.py:synthesize_to_file`,
`synthesis.py:try_direct_play`. A frozen dataclass bundles them:

- State: voice, language, rate, provider, model, stability, similarity, style,
  speaker_boost, api_key, once
- Methods: `validate()`, `to_client_kwargs() -> dict`
- Replaces: `_validate_voice_settings` (duplicated in __main__.py and server.py),
  the kwargs-building blocks, and the 14-parameter signatures

**Metrics:** avg_params, method_ratio, encapsulation_ratio

### Priority 2: Extract `unmute`/`record` shared logic in `server.py`

The `unmute` tool (150 lines) and `record` tool (130 lines) share 80% of their
logic. Extract `_synthesize_segments` helper. Reduces server.py by ~100 lines,
cuts max_complexity (currently 36), eliminates duplication.

**Metrics:** max_complexity, module_size

### Priority 3: `ConfigStore` class from `config.py`

6 top-level functions all take `config_dir` as parameter. A `ConfigStore` class
owns `_dir` and makes every function a method. Eliminates the
`config_dir or DEFAULT_CONFIG_DIR` pattern (4 occurrences). Makes config
injectable for testing.

**Metrics:** method_ratio, class_to_func_ratio, encapsulation_ratio

### Priority 4: Decompose `PlaybackQueue.play_audio`

155-line God Method. Split into `_validate_file`, `_compute_timeout`,
`_spawn_and_wait`, `_log_result`. All methods on PlaybackQueue — they share
`_last_result` state.

**Metrics:** max_complexity, avg_complexity

### Priority 5: `OutputFormatter` Strategy in `__main__.py`

Module-level mutable globals `_json_output`/`_quiet_output` control output
formatting. Replace with a Strategy class: `OutputFormatter` with
`emit(payload, text)` method.

**Metrics:** encapsulation_ratio, method_ratio

### Priority 6: Move provider voice caches onto instances

`ElevenLabsProvider` and `PollyProvider` use module-level mutable `VOICES` dicts.
Move to instance attributes `_voices`. Eliminates global mutable state, makes
testing cleaner.

**Metrics:** encapsulation_ratio, public_attr_violations

### Priority 7: `ApiKeyResolver` class in `__main__.py`

Four-source mutual-exclusion key resolution (arg, file, stdin, env).
Encapsulate in a class with `resolve() -> str | None`. Removes 115 lines of
procedural code.

**Metrics:** method_ratio, module_size

### Priority 8: `AudioMigration` class in `__main__.py`

117-line function with 5 responsibilities (scan, preview, execute, conflict
detect, cleanup). Natural class with `scan()`, `preview()`, `execute()`.

**Metrics:** max_complexity, method_ratio, module_size

### Priority 9: Extract chime resolution from `watcher.py`

~80 lines of chime resolution unrelated to session watching. Move to `chime.py`.

**Metrics:** module_size

### Priority 10: `ChunkedSynthesisMixin` or helper

ElevenLabs and OpenAI providers have identical chunking logic (~25 lines each).
Extract shared helper. Low priority — only 2 instances.

**Metrics:** code duplication

### Priority 11: `DaemonRestarter` class

166-line restart sequence (stop → wait → start → verify). Natural state machine
but only called from CLI. Lower testability audience.

**Metrics:** max_complexity, module_size

## Leave Alone

- **`core.py`**: Well-designed. `split_text` and `stitch_audio` are correct free
  functions.
- **`applet.py`**: Small, focused, pure.
- **`client.py` VoxClientSync duplication**: Mechanical, provides type safety.
  `__getattr__` would hurt readability.
- **`voxd/playback.py` free functions**: `_player_command`, `_probe_duration` etc.
  are genuinely stateless. Wrapping them would be metric-chasing.

## Per-File Detail

### `__main__.py` (1352 lines)

Domain concepts: CLI app, API key resolver, text segment resolver, audio
migration job, daemon lifecycle manager, status reporter. None are classes.

Key violations:
- `unmute` (lines 413–503): 7 responsibilities in one function
- `record` (lines 510–570): near-duplicate of `unmute`
- `daemon_restart_cmd` (lines 1358–1524): 166 lines, 6 responsibilities
- Module-level globals `_json_output`/`_quiet_output`

Patterns: Command (CLI subcommands), Builder (synthesis params), Strategy
(output formatting).

### `server.py` (977 lines)

Key violations:
- `unmute` and `record` share 80% logic — worst duplication in codebase
- `SessionConfig` has 11 public attributes with no validation on writes
- max_complexity 36 in one function

### `client.py` (625 lines)

5 classes, max_complexity 13, init_violations 2. Message-building repeats
identically across 10 methods (Builder pattern). VoxClientSync duplication is
acceptable.

### `config.py` (211 lines)

Entirely procedural — 6 functions all threading `config_dir`. Repository pattern
applies.

### `cache.py` (142 lines)

Procedural but each function is focused. `QuipCache` class would own `_dir` and
`_max_entries`. Moderate priority — current code works but uses implicit global.

### `voxd/synthesis.py` (407 lines)

Parameter bloat: `synthesize_to_file` takes 14 params, `try_direct_play` takes
13. `SynthesisSpec` reduces both to 2. Env-key injection is a context manager
waiting to be extracted.

### `voxd/playback.py` (346 lines)

`PlaybackQueue` is solid. Free functions are correct as functions. One God
Method: `play_audio` at 155 lines needs Extract Method.

### `watcher.py` (384 lines)

Mixes three concerns: JSONL parsing, chime resolution, watcher thread. Chime
resolution has no relationship to watching.

### Providers

Voice caches are module-level mutable globals. Chunking logic duplicated between
ElevenLabs and OpenAI.
