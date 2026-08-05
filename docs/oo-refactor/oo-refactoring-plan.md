# Refactoring Plan: punt-vox

Generated: 2026-05-13

This is document 3 of 3 in the voxd architecture redesign:

1. [OO Design Report](oo-design-report.md) — proposes 16 classes to replace the voxd.py monolith
2. [OO Design Review](oo-design-review.md) — reviews that proposal, adds 6 revisions bringing the total to 18 classes
3. **This document** — step-by-step execution plan implementing the reviewed design. 52 steps, tests green at every step.

Sources: oo-design-report.md (16 classes), oo-design-review.md (6 revisions)
Baseline: 2729-line voxd.py, 1151-line service.py, 992-line server.py, 1992-line `__main__.py`
Tests: 1444 passing (21s), including 4164-line test_voxd.py

## Calibration

The merchants/game codebase is the reference for what proper OO looks like
in Python at scale:

| Metric | merchants/game | punt-vox (current) | punt-vox (target) |
|--------|---------------|--------------------|--------------------|
| Max module LOC | 363 (captain.py) | 2729 (voxd.py) | <500 |
| Classes | ~20 | ~15 | 34 |
| LOC per class | ~100 | ~935 | ~150 |
| Module-level business logic | 0 | ~120 functions | 0 |

In merchants/game, `Game` is a Facade that creates `Deck`, `Captain`,
`RoundController`, and `SeazoneBuilder` via constructor injection. `Captain`
owns its cargo, gold, and glory. `Deck` owns its card list and discard pile.
No module-level business logic anywhere. Every method is on the class that
owns the data it operates on.

---

## Part 1: Final Architecture

### 1.1 Class Diagram (18 classes)

All 16 classes from the original report plus `TrackGenerator` and
`DoctorCheck` per reviewer revisions.

```text
VoxDaemon (composition root)                    ~120 LOC
  |-- DaemonConfig                              ~200 LOC
  |-- PlaybackQueue                             ~200 LOC
  |-- ChimeDedup (exists)                        ~30 LOC
  |-- OnceDedup (exists)                        ~100 LOC
  |-- ChimeResolver                              ~40 LOC
  |-- SynthesisPipeline                         ~350 LOC
  |-- MusicScheduler                            ~300 LOC
  |     '-- TrackGenerator                      ~150 LOC
  |-- DaemonHealth                               ~60 LOC
  |-- WebSocketRouter                           ~400 LOC
  |     |-- SynthesisPipeline
  |     |-- PlaybackQueue
  |     |-- MusicScheduler
  |     |-- ChimeDedup
  |     |-- OnceDedup
  |     |-- ChimeResolver
  |     '-- DaemonHealth

ServiceInstaller                                ~200 LOC
  |-- KeysEnvWriter                             ~100 LOC
  |-- ProcessManager                            ~150 LOC
  |-- LaunchdBackend                            ~150 LOC
  '-- SystemdBackend                            ~200 LOC

SessionConfig (refactor of SessionState)        ~100 LOC
DoctorCheck (extract from `__main__.py`)          ~300 LOC

```

### 1.2 Class Specifications

#### 1. PlaybackQueue

- **Module**: `src/punt_vox/voxd/playback.py`
- **Responsibility**: Owns the audio playback queue, consumer loop, playback mutex, and last-result snapshot.
- **Owns**: `_queue` (asyncio.Queue), `_mutex` (asyncio.Lock), `_last_result` (dict | None), `_consumer_task` (asyncio.Task | None)
- **Public interface**: `enqueue(item)`, `start_consumer()`, `stop_consumer()`, `last_result` property, `queue_size` property
- **Estimated LOC**: 200
- **Absorbs from voxd.py**: `PlaybackItem`, `_play_audio`, `_playback_consumer`, `_probe_duration`, `_record_playback_result`, `_truncate_stderr`, `_monotonic`, `_AUDIO_ENV_KEYS`, `_SUSPICIOUS_ELAPSED_S`, `_PLAYBACK_TIMEOUT_DEFAULT_S`, `_PLAYBACK_TIMEOUT_PADDING_S`, `_PROBE_TIMEOUT_S`, `_MAX_STDERR_LEN`, `_playback_mutex`
- **Does NOT absorb** (per reviewer): `_player_binary_path`, `_player_binary_name`, `_player_command`, `_snapshot_env`, `_is_darwin` -- these are platform audio utilities, not queue state. They stay as module-level pure functions in `voxd/playback.py` or a separate `voxd/platform.py`.
- **Dependencies**: None.

#### 2. SynthesisPipeline

- **Module**: `src/punt_vox/voxd/synthesis.py`
- **Responsibility**: Owns the synthesis process: vibe application, cache integration, provider construction, API key injection, env-lock serialization.
- **Owns**: `_env_lock` (asyncio.Lock)
- **Constructor parameters**: `cache_get: Callable`, `cache_put: Callable`, `cache_dir: Path` (per reviewer revision 6: constructor injection of cache interface, not module-level import)
- **Public interface**: `synthesize_to_file(...)` -> Path, `try_direct_play(...)` -> int | None | Exception
- **Estimated LOC**: 350
- **Absorbs from voxd.py**: `_synthesize_to_file`, `_try_direct_play`, `_run_play_directly_sync`, `_apply_vibe_for_synthesis`, `_model_supports_expressive_tags`, `_build_audio_request`, `_LOCAL_PROVIDERS`, `_PROVIDER_API_KEY_VAR`, `_env_lock`
- **Dependencies**: `punt_vox.cache` (via injected callables), `punt_vox.providers`, `punt_vox.core.TTSClient`, `punt_vox.normalize`

#### 3. MusicScheduler

- **Module**: `src/punt_vox/voxd/music_scheduler.py`
- **Responsibility**: Owns the music generation loop, track lifecycle, gapless handoff, and playlist state. Delegates generation to TrackGenerator.
- **Owns**: `_mode`, `_style`, `_owner`, `_vibe`, `_track`, `_track_name`, `_proc`, `_state`, `_changed`, `_replay`, `_loop_task`
- **Constructor parameters**: `track_generator: TrackGenerator`
- **Public interface**: `start_loop()`, `stop_loop()`, `turn_on(...)`, `turn_off()`, `play_track(...)`, `list_tracks()`, `update_vibe(...)`, `skip_next(...)`, `mode` property, `state` property, `track_name` property, `track` property, `owner` property, `vibe` property
- **Estimated LOC**: 300 (down from 480 after TrackGenerator extraction)
- **Absorbs from voxd.py**: `_music_loop`, `_playback_wait_loop`, `_kill_music_proc`, `_music_backoff_sleep`, `_PlaybackWaitResult`, `_MUSIC_DURATION_MS`, `_MUSIC_MAX_RETRIES`
- **Does NOT absorb** (per reviewer revision 1): `_handle_music_*` handlers -- wire-protocol parsing stays in WebSocketRouter. The scheduler exposes domain methods that the router calls.
- **Dependencies**: `TrackGenerator`, platform audio functions (`_music_player_command`)

#### 4. TrackGenerator (NEW -- reviewer revision 1)

- **Module**: `src/punt_vox/voxd/track_generator.py`
- **Responsibility**: Owns prompt construction, provider invocation, output directory management, and track naming. Pure async: takes parameters, returns a Path.
- **Owns**: `_output_dir` (Path)
- **Constructor parameters**: `output_dir: Path`
- **Public interface**: `generate(vibe, vibe_tags, style, track_name) -> Path`, `auto_track_name(vibe, style) -> str`, `list_tracks() -> list[dict]`
- **Estimated LOC**: 150
- **Absorbs from voxd.py**: `_generate_music_track`, `_auto_track_name`, `_slugify`, `_music_output_dir`
- **Dependencies**: `punt_vox.music.vibe_to_prompt`, `punt_vox.providers.elevenlabs_music.ElevenLabsMusicProvider`

#### 5. ChimeDedup (EXISTS)

- **Module**: `src/punt_vox/voxd/dedup.py`
- **Responsibility**: Always-on in-memory dedup for chime signals.
- **Owns**: `_window`, `_seen`
- **Public interface**: `should_play(signal) -> bool`
- **Estimated LOC**: 30
- **Move from**: voxd.py lines 739-764

#### 6. OnceDedup (EXISTS)

- **Module**: `src/punt_vox/voxd/dedup.py`
- **Responsibility**: Opt-in per-call speech dedup with caller-specified TTL.
- **Owns**: `_seen` dict
- **Public interface**: `check_and_record(text, ttl_seconds) -> DedupHit | None`, `rollback(text)`
- **Estimated LOC**: 100
- **Move from**: voxd.py lines 788-917, plus `DedupHit` (lines 767-786)

#### 7. ChimeResolver

- **Module**: `src/punt_vox/voxd/chimes.py`
- **Responsibility**: Resolves chime signal names to bundled asset paths. Immutable after construction.
- **Owns**: `_chime_map` dict
- **Constructor parameters**: None (reads `_CHIME_MAP` constant at class level)
- **Public interface**: `resolve(signal) -> Path | None`
- **Estimated LOC**: 40
- **Absorbs from voxd.py**: `_CHIME_MAP`, `_resolve_chime`
- **Dependencies**: `importlib.resources`

#### 8. WebSocketRouter

- **Module**: `src/punt_vox/voxd/router.py`
- **Responsibility**: WebSocket message dispatch, auth checking, client lifecycle tracking. Owns wire-protocol parsing for all message types including music handlers (per reviewer revision 1).
- **Owns**: `_handlers` dict, `_auth_token`, `_client_count`
- **Constructor parameters**: `synthesis: SynthesisPipeline`, `playback: PlaybackQueue`, `music: MusicScheduler`, `chime_dedup: ChimeDedup`, `once_dedup: OnceDedup`, `chimes: ChimeResolver`, `health: DaemonHealth`
- **Public interface**: `handle_connection(websocket)`, `client_count` property
- **Estimated LOC**: 400 (includes all `_handle_*` functions as private methods)
- **Absorbs from voxd.py**: `_ws_route`, `_check_auth`, `_HANDLERS`, `_handle_synthesize`, `_handle_record`, `_handle_chime`, `_handle_voices`, `_handle_health`, `_handle_music_on`, `_handle_music_off`, `_handle_music_play`, `_handle_music_list`, `_handle_music_vibe`, `_handle_music_next`, `_parse_optional_float`, `_parse_optional_int`, `_parse_optional_str`
- **Dependencies**: All Phase 1 and Phase 2 classes (via constructor injection)

#### 9. DaemonHealth

- **Module**: `src/punt_vox/voxd/health.py`
- **Responsibility**: Computes health payloads for authenticated and unauthenticated callers.
- **Owns**: `_start_time`, `_daemon_version`, `_port`
- **Constructor parameters**: `playback: PlaybackQueue`, `get_client_count: Callable[[], int]` (per reviewer revision 2: callable instead of WebSocketRouter reference, breaking the circular dependency)
- **Public interface**: `minimal_payload() -> dict`, `full_payload() -> dict`
- **Estimated LOC**: 60
- **Absorbs from voxd.py**: `_health_payload_minimal`, `_health_payload_full`
- **Dependencies**: `PlaybackQueue` (for queue size). Uses a callable for client count instead of depending on WebSocketRouter directly.

#### 10. DaemonConfig

- **Module**: `src/punt_vox/voxd/config.py`
- **Responsibility**: Startup configuration: key loading, auth token management, port file lifecycle, logging setup.
- **Owns**: `_run_dir`, `_config_dir`, `_log_dir`, `_auth_token`, `_port`
- **Public interface**: `load_keys()`, `read_or_create_token()`, `write_port_file(port)`, `remove_port_file()`, `configure_logging()`, `auth_token` property, `port` property
- **Estimated LOC**: 200
- **Absorbs from voxd.py**: `_load_keys`, `_read_or_create_token`, `_write_port_file`, `_remove_port_file`, `_configure_logging`, `_log_voxd_environment`, `_TokenRedactFilter`, `_install_token_redact_filter`, logging constants (`_LOG_FORMAT`, etc.), `_STARTUP_ENV_KEYS`
- **Does NOT absorb**: `_PROVIDER_KEY_NAMES` -- imported from shared constant (see revision 3)
- **Dependencies**: `punt_vox.keys.PROVIDER_KEY_NAMES` (shared constant)

#### 11. VoxDaemon (replaces DaemonContext)

- **Module**: `src/punt_vox/voxd/daemon.py`
- **Responsibility**: Composition root. Wires domain objects together, owns the Starlette app lifecycle, starts/stops background tasks.
- **Owns**: References to all domain objects (via constructor injection)
- **Public interface**: `build_app() -> Starlette`, `run(host, port)`
- **Estimated LOC**: 120
- **Absorbs from voxd.py**: `build_app`, `main`, `_health_route`, lifespan function, uvicorn configuration
- **Dependencies**: All voxd/* classes

#### 12. ServiceInstaller

- **Module**: `src/punt_vox/service/installer.py`
- **Responsibility**: Cross-platform daemon service installation and uninstallation.
- **Owns**: `_platform`, `_user`, `_state_root`
- **Public interface**: `install() -> str`, `uninstall() -> str`, `is_running() -> bool`, `detect_platform() -> str`
- **Estimated LOC**: 200
- **Absorbs from service.py**: `install`, `uninstall`, `is_running`, `detect_platform`, `_ensure_user_dirs`, `_voxd_exec_args`, `DEFAULT_PORT`, `_SUDO_NOTICE`

#### 13. KeysEnvWriter

- **Module**: `src/punt_vox/service/keys_env.py`
- **Responsibility**: Reads and writes keys.env with merge semantics and secure file creation.
- **Owns**: `_keys_path`
- **Constructor parameters**: `keys_path: Path`
- **Public interface**: `write(env) -> Path`
- **Estimated LOC**: 100
- **Absorbs from service.py**: `_write_keys_env`
- **Uses**: `punt_vox.keys.PROVIDER_KEY_NAMES` (shared constant, per reviewer revision 3)

#### 14. ProcessManager

- **Module**: `src/punt_vox/service/process.py`
- **Responsibility**: Find and kill daemon processes by port and read/write port files.
- **Owns**: `_default_port`
- **Public interface**: `find_pid_on_port(port)`, `is_vox_daemon_process(pid)`, `kill_pid(pid)`, `kill_stale_daemon()`, `ensure_port_free()`, `read_port_file()`, `remove_port_file()`
- **Estimated LOC**: 150
- **Absorbs from service.py**: `_find_pid_on_port`, `_is_vox_daemon_process`, `_kill_pid`, `_kill_stale_daemon`, `_ensure_port_free`, `read_port_file`, `_remove_port_file`

#### 15. LaunchdBackend

- **Module**: `src/punt_vox/service/launchd.py`
- **Responsibility**: macOS launchd plist generation and load/unload/kickstart.
- **Owns**: `_label`, `_plist_path`
- **Public interface**: `plist_content(user)`, `stop()`, `install(user)`, `uninstall()`, `is_running()`
- **Estimated LOC**: 150
- **Absorbs from service.py**: `_launchd_plist_content`, `_launchd_stop`, `_launchd_install`, `_launchd_uninstall`, `_launchd_status`, `_extra_launchd_env`, `_LAUNCHD_DIR`, `_LAUNCHD_PLIST`, `_LABEL`

#### 16. SystemdBackend

- **Module**: `src/punt_vox/service/systemd.py`
- **Responsibility**: Linux systemd unit generation and start/stop/enable/restart.
- **Owns**: `_unit_path`
- **Public interface**: `unit_content(user)`, `stop()`, `install(user)`, `uninstall()`, `is_running()`, `cleanup_stale_user_unit()`
- **Estimated LOC**: 200
- **Absorbs from service.py**: `_systemd_unit_content`, `_systemd_stop`, `_systemd_install`, `_systemd_uninstall`, `_systemd_status`, `_systemd_audio_env_lines`, `_safe_systemd_value`, `_cleanup_stale_user_unit`, `_legacy_user_unit_path`, `_LEGACY_USER_UNIT_RELATIVE`, `_SYSTEMD_DIR`, `_SYSTEMD_UNIT`

#### 17. SessionConfig (refactor of SessionState -- reviewer revision 4)

- **Module**: `src/punt_vox/server.py` (in-place refactor)
- **Responsibility**: MCP session state. Owns refresh logic and field-update logic as methods, not external functions.
- **Owns**: All current `SessionState` fields plus `_config_dir`
- **Public interface**: `refresh_from_config()`, `update_provider(provider)`, `update_model(model)`, `update_vibe_tags(tags)`, `effective_voice(override)`, `effective_provider(override)`, `to_status_dict()`
- **Estimated LOC**: 100
- **Key change**: The module-level `_state` singleton is eliminated. `SessionConfig` is instantiated once in `run_server()` and passed to tool functions via FastMCP's context or a module-level reference that is a proper instance, not a mutable bag mutated by external functions.

#### 18. DoctorCheck (NEW -- reviewer revision 5)

- **Module**: `src/punt_vox/doctor.py`
- **Responsibility**: All diagnostic checks currently in `__main__.py`'s `doctor` command. Each check is a method returning a structured result.
- **Owns**: Diagnostic state
- **Public interface**: `run_all() -> list[CheckResult]`, individual check methods
- **Estimated LOC**: 300
- **Absorbs from **main**.py**: The ~300 lines of `doctor` command logic

### 1.3 Shared Constants (reviewer revision 3)

`_PROVIDER_KEY_NAMES` is currently duplicated in three files:

- `src/punt_vox/voxd.py` line 258
- `src/punt_vox/service.py` line 81
- `src/punt_vox/keys.py` line 22

The canonical location is `src/punt_vox/keys.py`. Rename from `_PROVIDER_KEY_NAMES` to `PROVIDER_KEY_NAMES` (public) and have `voxd/config.py` and `service/keys_env.py` import from there. The two duplicates are deleted.

### 1.4 Dependency Graph

```text
Phase 1 (no dependencies):
  ChimeDedup, OnceDedup, DedupHit     ->  voxd/dedup.py
  ChimeResolver                        ->  voxd/chimes.py
  PlaybackQueue                        ->  voxd/playback.py
  DaemonConfig                         ->  voxd/config.py
  TrackGenerator                       ->  voxd/track_generator.py

Phase 2 (depends on Phase 1):
  SynthesisPipeline                    ->  voxd/synthesis.py
  MusicScheduler                       ->  voxd/music_scheduler.py

Phase 3 (depends on Phase 1 + Phase 2):
  DaemonHealth                         ->  voxd/health.py
  WebSocketRouter                      ->  voxd/router.py

Phase 4 (composition root):
  VoxDaemon                            ->  voxd/daemon.py
  voxd.py -> voxd/`__init__.py`          (re-exports entrypoint)

Phase 5 (service.py -- independent of voxd):
  ProcessManager                       ->  service/process.py
  KeysEnvWriter                        ->  service/keys_env.py
  LaunchdBackend                       ->  service/launchd.py
  SystemdBackend                       ->  service/systemd.py
  ServiceInstaller                     ->  service/installer.py

Phase 6 (server.py + `__main__.py` -- independent):
  SessionConfig                        ->  server.py (in-place)
  DoctorCheck                          ->  doctor.py

```

No cycles. Every dependency points downward. `DaemonHealth` is in Phase 3
(not Phase 2) per reviewer revision 2 -- it depends on `PlaybackQueue`
(Phase 1) and needs a client-count callable that comes from `WebSocketRouter`
(Phase 3). The circular dependency is broken by injecting a callable
`get_client_count: Callable[[], int]` instead of a reference to the router.

### 1.5 Reviewer Revisions -- Disposition

| # | Revision | Status |
|---|----------|--------|
| 1 | Split MusicScheduler into MusicScheduler (300) + TrackGenerator (150). Move `_handle_music_*` wire-protocol parsing to WebSocketRouter. | ADOPTED. TrackGenerator is class #4. All `_handle_music_*` go to WebSocketRouter. |
| 2 | Move DaemonHealth to Phase 3 (depends on WebSocketRouter for client count). | ADOPTED. DaemonHealth in Phase 3. Dependency broken via callable injection. |
| 3 | Extract `_PROVIDER_KEY_NAMES` to shared location. | ADOPTED. Canonical location: `punt_vox.keys.PROVIDER_KEY_NAMES`. |
| 4 | Eliminate module-level `_state` singleton in server.py. | ADOPTED. SessionConfig owns its own refresh logic as methods. |
| 5 | Extract DoctorCheck from `__main__.py`. | ADOPTED. Class #18, `src/punt_vox/doctor.py`. |
| 6 | SynthesisPipeline receives cache interface via constructor injection. | ADOPTED. Constructor takes `cache_get`, `cache_put`, `cache_dir` callables. |

---

## Part 2: Refactoring Steps

### Design Principle

**Every step produces a finished class.** Not "move functions to a file."
A class with a constructor, owned state, public methods, and constructor
injection. The functions become methods in the same step they move.

**DaemonContext fields move to the owning class in the same step.**
When PlaybackQueue is created, `playback_queue`, `last_playback`, and
`_playback_mutex` move OFF DaemonContext and INTO PlaybackQueue.
DaemonContext shrinks at every step. By the end, DaemonContext is gone.

**Backward compatibility during transition.** When a field moves off
DaemonContext, add a property on DaemonContext that delegates to the
new class. Old code still works through DaemonContext until it is
updated. This includes test code that accesses `ctx.last_playback`.

**Mock targets update in the same step.** When PlaybackQueue is
created, tests that test playback move to a new file and mock against
the new class.

**No "cleanup later" steps.** Every step is complete.

### Prerequisites

Before any step, confirm the baseline:

```bash
make check   # must show all passing (current count)

```

Every step ends with `make check`. Zero failures at every step.

---

### Step 0: Extract shared PROVIDER_KEY_NAMES constant

**What changes**: `_PROVIDER_KEY_NAMES` in `keys.py` is renamed to `PROVIDER_KEY_NAMES` (public).
`_PROVIDER_KEY_NAMES` definitions in `voxd.py` (line 257-269) and `service.py` (line 80-92)
are replaced with imports from `punt_vox.keys`.

**Changes**:

1. In `src/punt_vox/keys.py`: rename `_PROVIDER_KEY_NAMES` to `PROVIDER_KEY_NAMES` (remove leading underscore). Update all internal references in `keys.py`.
2. In `src/punt_vox/voxd.py`: delete the `_PROVIDER_KEY_NAMES` definition. Add `from punt_vox.keys import PROVIDER_KEY_NAMES`. Replace use at line 300 (`if key in _PROVIDER_KEY_NAMES`) with `if key in PROVIDER_KEY_NAMES`.
3. In `src/punt_vox/service.py`: delete the `_PROVIDER_KEY_NAMES` definition. Add `from punt_vox.keys import PROVIDER_KEY_NAMES`. Replace use.
4. In `tests/test_keys.py`: if it references `_PROVIDER_KEY_NAMES`, update import to `PROVIDER_KEY_NAMES`.

**Test impact**: Minimal. The constant value is unchanged.

**Verification**: `make check`

---

### Step 1: Create `voxd/` package and the `DaemonConfig` class

**Class being created**: `DaemonConfig`
**Module**: `src/punt_vox/voxd/config.py`

**Constructor**:

```python
class DaemonConfig:
    def __new__(cls, run_dir: Path, config_dir: Path, log_dir: Path) -> Self:
        self = super().__new__(cls)
        self._run_dir = run_dir
        self._config_dir = config_dir
        self._log_dir = log_dir
        self._auth_token: str | None = None
        return self

```

**Public methods**: `load_keys() -> frozenset[str]`, `read_or_create_token() -> str`, `write_port_file(port)`, `remove_port_file()`, `configure_logging()`, `log_environment()`, `auth_token` property, plus `read_port_file()` and `read_token_file()` as classmethods.

**Functions that become methods** (from `voxd.py`):

- `_load_keys` -> `DaemonConfig.load_keys()`
- `_read_or_create_token` -> `DaemonConfig.read_or_create_token()`
- `_write_port_file` -> `DaemonConfig.write_port_file()`
- `_remove_port_file` -> `DaemonConfig.remove_port_file()`
- `_configure_logging` -> `DaemonConfig.configure_logging()`
- `_log_voxd_environment` -> `DaemonConfig.log_environment()`
- `read_port_file` -> `DaemonConfig.read_port_file()` (classmethod)
- `read_token_file` -> `DaemonConfig.read_token_file()` (classmethod)

**Constants that move**: `_LOG_FORMAT`, `_LOG_DATE_FORMAT`, `_LOG_MAX_BYTES`, `_LOG_BACKUP_COUNT`, `_STARTUP_ENV_KEYS`, `_TOKEN_RE`, `_TokenRedactFilter`, `_install_token_redact_filter`.

**Path wrappers that move**: `_config_dir()`, `_log_dir()`, `_run_dir()`.

**State that moves off DaemonContext**: None yet. DaemonContext still holds `auth_token` and `port`. These are set by `DaemonConfig` and passed to DaemonContext at construction in `main()`.

**DaemonContext delegations added**: None needed. The config fields were never on DaemonContext in the mutable sense -- `auth_token` is set once at startup.

**Package creation** (simultaneous):

1. Create `src/punt_vox/voxd/` directory.
2. Move `src/punt_vox/voxd.py` to `src/punt_vox/voxd/_monolith.py`.
3. Create `src/punt_vox/voxd/__init__.py` that re-exports everything currently exported by `voxd.py` from `_monolith`.
4. Create `src/punt_vox/voxd/config.py` with the `DaemonConfig` class.
5. In `_monolith.py`: delete the moved functions/constants. Import `DaemonConfig` from `voxd.config`. Update `main()` to construct `DaemonConfig` and call its methods.
6. In `__init__.py`: re-export `DaemonConfig` and the public functions (`read_port_file`, `read_token_file`).

**Mock target changes**: ALL `patch("punt_vox.voxd.X", ...)` in `test_voxd.py` become `patch("punt_vox.voxd._monolith.X", ...)`. This is the largest single test change in the plan and happens in this step.

**Tests that move**: `TestVoxdPaths`, `TestLoadKeys`, `TestVoxdStartupEnforces0700`, `TestVoxdPathHelpersArePure` move to `tests/test_voxd_config.py` and test against `DaemonConfig` methods directly. Mock targets become `"punt_vox.voxd.config.X"`.

**Verification**: `make check`

---

### Step 2: Create the `ChimeResolver` class

**Class being created**: `ChimeResolver`
**Module**: `src/punt_vox/voxd/chimes.py`

**Constructor**:

```python
class ChimeResolver:
    _CHIME_MAP: ClassVar[dict[str, str]] = { ... }

    def resolve(self, signal: str) -> Path | None:
        # body of _resolve_chime

```

**Functions that become methods** (from `_monolith.py`):

- `_resolve_chime` -> `ChimeResolver.resolve()`

**Constants that move**: `_CHIME_MAP`

**State that moves off DaemonContext**: None (chime resolution was never on DaemonContext).

**DaemonContext delegations added**: None.

**Backward compat**: A module-level `_resolve_chime(signal)` function in `_monolith.py` is replaced with `from punt_vox.voxd.chimes import ChimeResolver; _chime_resolver = ChimeResolver()` and all calls become `_chime_resolver.resolve(signal)`.

**Tests that move**: Any chime-specific tests move to `tests/test_voxd_chimes.py`. If none exist separately, this is a new small test file that tests `ChimeResolver` directly.

**Mock target changes**: If tests mock `_resolve_chime`, they now mock `ChimeResolver.resolve` or the `_chime_resolver` instance in `_monolith.py`.

**Verification**: `make check`

---

### Step 3: Move `ChimeDedup`, `OnceDedup`, `DedupHit` to `voxd/dedup.py`

**Classes being moved**: `ChimeDedup`, `OnceDedup`, `DedupHit` (already classes)
**Module**: `src/punt_vox/voxd/dedup.py`

**What moves**: The three classes and their associated constants (`_DEDUP_WINDOW_SECONDS`, `_ONCE_DEDUP_MAX_TTL_SECONDS`, `_ONCE_DEDUP_MAX_ENTRIES`) from `_monolith.py` to `dedup.py`.

**State on DaemonContext**: `chime_dedup` and `once_dedup` remain on DaemonContext for now. They are already proper classes -- DaemonContext is just holding references. These references move off DaemonContext when WebSocketRouter is created (Step 9).

**DaemonContext delegations added**: None needed -- tests already construct `ChimeDedup()` and `OnceDedup()` directly.

**Tests that move**: `TestOnceDedup`, `TestChimeDedup` move to `tests/test_voxd_dedup.py`. These tests construct the classes directly, so no mock target changes needed. The `monkeypatch.setattr("punt_vox.voxd.time.monotonic", ...)` calls change to `monkeypatch.setattr("punt_vox.voxd.dedup.time.monotonic", ...)`.

**Verification**: `make check`

---

### Step 4: Create the `PlaybackQueue` class

**Class being created**: `PlaybackQueue`
**Module**: `src/punt_vox/voxd/playback.py`

**Constructor**:

```python
class PlaybackQueue:
    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._queue: asyncio.Queue[PlaybackItem] = asyncio.Queue()
        self._mutex = asyncio.Lock()
        self._last_result: dict[str, object] | None = None
        return self

    @property
    def last_result(self) -> dict[str, object] | None: ...

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    async def enqueue(self, item: PlaybackItem) -> None: ...
    async def start_consumer(self) -> asyncio.Task[None]: ...
    async def play_audio(self, path: Path) -> None: ...

```

**Functions that become methods** (from `_monolith.py`):

- `_play_audio(path, ctx)` -> `PlaybackQueue.play_audio(path)` (no more ctx -- records result on `self._last_result`)
- `_playback_consumer(ctx)` -> `PlaybackQueue._consumer()` (reads from `self._queue`, writes to `self._last_result`)
- `_record_playback_result(ctx, ...)` -> `PlaybackQueue._record_result(...)` (writes to `self._last_result`)
- `_probe_duration(path)` -> stays as module-level async function in `playback.py` (pure, no state)
- `_truncate_stderr`, `_monotonic`, `_snapshot_env` -> stay as module-level pure functions in `playback.py`
- `_is_darwin`, `_player_binary_name`, `_player_binary_path`, `_player_command` -> stay as module-level pure functions in `playback.py`
- `_music_player_command` -> stays as module-level pure function in `playback.py`

**Constants that move**: `PlaybackItem`, `_AUDIO_ENV_KEYS`, `_SUSPICIOUS_ELAPSED_S`, `_PLAYBACK_TIMEOUT_DEFAULT_S`, `_PLAYBACK_TIMEOUT_PADDING_S`, `_PROBE_TIMEOUT_S`, `_MAX_STDERR_LEN`, `_MUSIC_VOLUME`

**State that moves off DaemonContext**:

- `playback_queue: asyncio.Queue[PlaybackItem]` -> removed from DaemonContext
- `last_playback: dict[str, object] | None` -> removed from DaemonContext

**DaemonContext delegations added** (for backward compat during transition):

```python
@property
def playback_queue(self) -> asyncio.Queue[PlaybackItem]:
    return self._playback.queue  # delegate to PlaybackQueue

@property
def last_playback(self) -> dict[str, object] | None:
    return self._playback.last_result

@last_playback.setter
def last_playback(self, value: dict[str, object] | None) -> None:
    self._playback.set_last_result(value)

```

`PlaybackQueue` exposes a `set_last_result(value)` method for test use.
The delegation setter calls this method -- it never reaches into
`PlaybackQueue._last_result` directly.

DaemonContext constructor changes: accepts `playback: PlaybackQueue` instead of creating its own queue. The `_playback_mutex` module-level lock is eliminated -- the mutex now lives inside `PlaybackQueue`.

**Mock target changes**:

- `"punt_vox.voxd._monolith.asyncio.create_subprocess_exec"` in playback tests -> `"punt_vox.voxd.playback.asyncio.create_subprocess_exec"`
- `"punt_vox.voxd._monolith._monotonic"` -> `"punt_vox.voxd.playback._monotonic"`
- `"punt_vox.voxd._monolith._probe_duration"` -> `"punt_vox.voxd.playback._probe_duration"`
- `"punt_vox.voxd._monolith.asyncio.wait_for"` in playback tests -> `"punt_vox.voxd.playback.asyncio.wait_for"`
- `"punt_vox.voxd._monolith._player_binary_path"` -> `"punt_vox.voxd.playback._player_binary_path"`
- `"punt_vox.voxd._monolith._is_darwin"` -> `"punt_vox.voxd.playback._is_darwin"`

**Tests that move**: `TestPlayAudioObservability`, `TestProbeDuration`, `TestPlayAudioProportionalTimeout`, `TestMusicPlayerCommand`, `TestStderrTruncation`, `TestMusicSeparateFromPlaybackQueue` move to `tests/test_voxd_playback.py`.

Tests that call `_play_audio(audio, ctx)` now call `playback_queue.play_audio(audio)` and check `playback_queue.last_result` instead of `ctx.last_playback`.

**Verification**: `make check`

---

### Step 5: Create the `TrackGenerator` class

**Class being created**: `TrackGenerator`
**Module**: `src/punt_vox/voxd/track_generator.py`

**Constructor**:

```python
class TrackGenerator:
    def __new__(cls, output_dir: Path) -> Self:
        self = super().__new__(cls)
        self._output_dir = output_dir
        return self

    async def generate(
        self, vibe: tuple[str, str], style: str, track_name: str
    ) -> tuple[Path, str]:
        """Returns (track_path, resolved_track_name)."""

    def auto_track_name(self, vibe: str, style: str) -> str: ...

    def list_tracks(self) -> list[dict[str, object]]: ...

    @staticmethod
    def slugify(text: str, max_len: int = 40) -> str: ...

```

**Functions that become methods** (from `_monolith.py`):

- `_generate_music_track(ctx)` -> `TrackGenerator.generate(vibe, style, track_name)` (takes explicit params instead of ctx)
- `_auto_track_name(ctx)` -> `TrackGenerator.auto_track_name(vibe, style)` (takes explicit params instead of ctx)
- `_slugify(text)` -> `TrackGenerator.slugify(text)` (staticmethod)
- `_music_output_dir()` -> eliminated; the output dir is a constructor parameter

**State that moves off DaemonContext**: None directly. `music_track_name` stays on DaemonContext until MusicScheduler is created (Step 7). The caller (currently `_music_loop` in `_monolith.py`) adapts by passing `ctx.music_vibe`, `ctx.music_style`, `ctx.music_track_name` to `TrackGenerator.generate()` and storing the returned name back to `ctx.music_track_name`.

**The `_handle_music_list` handler** also moves its track-listing logic to `TrackGenerator.list_tracks()`.

**Mock target changes**:

- `"punt_vox.voxd._monolith._music_output_dir"` -> `"punt_vox.voxd.track_generator.TrackGenerator._output_dir"` or the constructor param
- `"punt_vox.providers.elevenlabs_music.ElevenLabsMusicProvider.generate_track"` -> unchanged (this patches the provider, not voxd)

**Tests that move**: `TestAutoTrackName`, `TestDaemonContextTrackName` move to `tests/test_voxd_track_gen.py`. The `TestAutoTrackName` tests change from calling `_auto_track_name(ctx)` (where ctx holds vibe/style) to calling `generator.auto_track_name(vibe, style)` with explicit parameters.

**Verification**: `make check`

---

### Step 6: Create the `SynthesisPipeline` class

**Class being created**: `SynthesisPipeline`
**Module**: `src/punt_vox/voxd/synthesis.py`

**Constructor**:

```python
class SynthesisPipeline:
    def __new__(
        cls,
        cache_get: Callable[..., Path | None],
        cache_put: Callable[..., None],
        cache_dir: Path,
        playback_mutex: asyncio.Lock,
    ) -> Self:
        self = super().__new__(cls)
        self._cache_get = cache_get
        self._cache_put = cache_put
        self._cache_dir = cache_dir
        self._playback_mutex = playback_mutex
        self._env_lock = asyncio.Lock()
        return self

```

**Public methods**: `synthesize_to_file(...)` -> Path, `try_direct_play(...)` -> int | None | Exception

**Functions that become methods** (from `_monolith.py`):

- `_synthesize_to_file(...)` -> `SynthesisPipeline.synthesize_to_file(...)`
- `_try_direct_play(*, ..., ctx)` -> `SynthesisPipeline.try_direct_play(*, ..., record_result: Callable)` (takes a callback instead of ctx)
- `_run_play_directly_sync(...)` -> `SynthesisPipeline._run_play_directly_sync(...)` (private method or stays as module-level pure function)
- `_build_audio_request(...)` -> stays as module-level pure function in `synthesis.py`

**Static methods**: `apply_vibe_for_synthesis(...)`, `model_supports_expressive_tags(...)`

**Constants that move**: `_LOCAL_PROVIDERS`, `_PROVIDER_API_KEY_VAR`

**State that moves off DaemonContext**: None directly. `_env_lock` and `_playback_mutex` were module-level, not on DaemonContext. The `_env_lock` is now owned by `SynthesisPipeline`. The `_playback_mutex` is injected from `PlaybackQueue`.

**The `_try_direct_play` function** currently takes `ctx: DaemonContext` only to call `_record_playback_result(ctx, ...)`. In the new design, it takes a `record_result: Callable` parameter. The caller (WebSocketRouter, created in Step 9) passes `playback_queue._record_result` or a lambda.

**DaemonContext delegations added**: None.

**Mock target changes**:

- `"punt_vox.voxd._monolith.cache_get"` -> `"punt_vox.voxd.synthesis.SynthesisPipeline._cache_get"` or inject fake callables via constructor
- `"punt_vox.voxd._monolith.cache_put"` -> same pattern
- `"punt_vox.voxd._monolith.get_provider"` (in synthesis tests) -> `"punt_vox.voxd.synthesis.get_provider"`
- `"punt_vox.voxd._monolith.normalize_for_speech"` (in synthesis tests) -> `"punt_vox.voxd.synthesis.normalize_for_speech"`
- `"punt_vox.voxd._monolith._cache_module"` -> `"punt_vox.voxd.synthesis._cache_module"` or eliminated (cache_dir injected via constructor)
- `"punt_vox.voxd._monolith._try_direct_play"` -> `"punt_vox.voxd._monolith._synthesis.try_direct_play"` or import alias in `_monolith.py`
- `"punt_vox.voxd._monolith._synthesize_to_file"` -> import alias in `_monolith.py` (handlers still in `_monolith.py` at this point)

**Tests that move**: `TestTryDirectPlay`, `TestDirectPlayProtocol`, `TestDirectPlaySerialization`, `TestApiKeyPassthroughIntegration`, `TestCacheApiKeyBypass`, `TestSynthesizeFailFast`, `TestModelSupportsExpressiveTags`, `TestApplyVibeForSynthesis` move to `tests/test_voxd_synthesis.py`.

Tests that called `_try_direct_play(..., ctx=ctx)` now construct a `SynthesisPipeline` instance with injected fakes and call `pipeline.try_direct_play(..., record_result=some_callback)`. Tests that checked `ctx.last_playback` now check the callback's captured data.

**Verification**: `make check`

---

### Step 7: Create the `MusicScheduler` class

**Class being created**: `MusicScheduler`
**Module**: `src/punt_vox/voxd/music_scheduler.py`

**Constructor**:

```python
class MusicScheduler:
    def __new__(cls, track_generator: TrackGenerator) -> Self:
        self = super().__new__(cls)
        self._generator = track_generator
        self._mode: str = "off"
        self._style: str = ""
        self._owner: str = ""
        self._vibe: tuple[str, str] = ("", "")
        self._track: Path | None = None
        self._track_name: str = ""
        self._proc: asyncio.subprocess.Process | None = None
        self._state: str = "idle"
        self._changed: asyncio.Event = asyncio.Event()
        self._replay: bool = False
        self._loop_task: asyncio.Task[None] | None = None
        return self

```

**Public methods** (domain interface for the router to call):

- `start_loop()`, `stop_loop()`
- `turn_on(owner_id, style, vibe, vibe_tags, name) -> dict` (returns response payload)
- `turn_off() -> dict`
- `play_track(owner_id, name) -> dict`
- `update_vibe(owner_id, vibe, vibe_tags) -> dict`
- `skip_next(owner_id) -> dict`
- Properties: `mode`, `state`, `track_name`, `track`, `owner`, `vibe`

**Functions that become methods** (from `_monolith.py`):

- `_music_loop(ctx)` -> `MusicScheduler._loop()` (reads `self._mode` etc. instead of `ctx.music_mode`)
- `_playback_wait_loop(ctx, proc, ...)` -> `MusicScheduler._playback_wait_loop(proc, ...)`
- `_kill_music_proc(ctx)` -> `MusicScheduler._kill_proc()`
- `_music_backoff_sleep(seconds, ctx)` -> `MusicScheduler._backoff_sleep(seconds)`

**Constants that move**: `_MUSIC_DURATION_MS`, `_MUSIC_MAX_RETRIES`, `_PlaybackWaitResult`

**State that moves off DaemonContext**:

- `music_mode` -> `MusicScheduler._mode`
- `music_style` -> `MusicScheduler._style`
- `music_owner` -> `MusicScheduler._owner`
- `music_vibe` -> `MusicScheduler._vibe`
- `music_track` -> `MusicScheduler._track`
- `music_track_name` -> `MusicScheduler._track_name`
- `music_proc` -> `MusicScheduler._proc`
- `music_state` -> `MusicScheduler._state`
- `music_changed` -> `MusicScheduler._changed`
- `music_replay` -> `MusicScheduler._replay`

**DaemonContext delegations added** (for backward compat):

```python
@property
def music_mode(self) -> str: return self._music.mode
@music_mode.setter
def music_mode(self, val: str) -> None: self._music.mode = val
# ... repeat for all 10 music fields

```

`MusicScheduler` exposes writable properties for `mode`, `style`, `owner`,
`vibe`, `track`, `track_name`, `proc`, `state`, `changed`, and `replay`.
The delegation setters on DaemonContext call these properties -- they never
reach into `MusicScheduler._private` attributes directly.

These delegations keep the existing handler tests working until the handlers move to WebSocketRouter (Step 9). At Step 9, the delegations are removed.

**Mock target changes**:

- `"punt_vox.voxd._monolith.asyncio.create_subprocess_exec"` in music tests -> `"punt_vox.voxd.music_scheduler.asyncio.create_subprocess_exec"`
- `"punt_vox.voxd._monolith._music_backoff_sleep"` -> `"punt_vox.voxd.music_scheduler.MusicScheduler._backoff_sleep"`
- `"punt_vox.voxd._monolith._music_output_dir"` -> eliminated (TrackGenerator._output_dir is injected)

**Tests that move**: `TestDaemonContextMusicFields`, `TestMusicLoopStateTransitions`, `TestMusicLoopGaplessHandoff`, `TestKillMusicProc`, `TestMusicLoopLostWakeup`, `TestGenFailureKeepsOldTrack` move to `tests/test_voxd_music.py`.

Tests that construct `_make_ctx()` and set `ctx.music_mode` etc. now construct a `MusicScheduler` directly and call its methods. Tests that drive `_music_loop(ctx)` now call `scheduler.start_loop()` / `scheduler.stop_loop()`.

**Verification**: `make check`

---

### Step 8: Create the `DaemonHealth` class

**Class being created**: `DaemonHealth`
**Module**: `src/punt_vox/voxd/health.py`

**Constructor**:

```python
class DaemonHealth:
    def __new__(
        cls,
        playback: PlaybackQueue,
        get_client_count: Callable[[], int],
        port: int,
    ) -> Self:
        self = super().__new__(cls)
        self._playback = playback
        self._get_client_count = get_client_count
        self._start_time = time.monotonic()
        self._port = port
        self._daemon_version = installed_version()
        return self

```

**Public methods**: `minimal_payload() -> dict`, `full_payload() -> dict`

**Functions that become methods** (from `_monolith.py`):

- `_health_payload_minimal(ctx)` -> `DaemonHealth.minimal_payload()` (reads `self._playback.queue_size`, `self._get_client_count()`, `self._port`)
- `_health_payload_full(ctx)` -> `DaemonHealth.full_payload()` (adds `self._playback.last_result`, `self._daemon_version`, `os.getpid()`)

**State that moves off DaemonContext**:

- `start_time` -> `DaemonHealth._start_time`
- `daemon_version` -> `DaemonHealth._daemon_version`
- `port` -> `DaemonHealth._port` (also still on DaemonContext for other uses until Step 10)
- `client_count` -> stays on DaemonContext until WebSocketRouter owns it (Step 9)

**DaemonContext delegations added**:

```python
@property
def daemon_version(self) -> str: return self._health.daemon_version
@daemon_version.setter
def daemon_version(self, val: str) -> None: self._health.set_daemon_version(val)

```

`DaemonHealth` exposes a `set_daemon_version(val)` method for test use.
The delegation setter calls this method -- it never reaches into
`DaemonHealth._daemon_version` directly.

**Tests that move**: `TestHealthPayloadFull`, `TestHealthPayloadMinimal` move to `tests/test_voxd_health.py`. Tests now construct `DaemonHealth` directly with a fake `PlaybackQueue` and a lambda for `get_client_count`. Tests that set `ctx.daemon_version = "99.99.99-test-sentinel"` now call `health.set_daemon_version("99.99.99-test-sentinel")`.

**Verification**: `make check`

---

### Step 9: Create the `WebSocketRouter` class

**Class being created**: `WebSocketRouter`
**Module**: `src/punt_vox/voxd/router.py`

**Constructor**:

```python
class WebSocketRouter:
    def __new__(
        cls,
        *,
        synthesis: SynthesisPipeline,
        playback: PlaybackQueue,
        music: MusicScheduler,
        chime_dedup: ChimeDedup,
        once_dedup: OnceDedup,
        chimes: ChimeResolver,
        health: DaemonHealth,
        auth_token: str | None,
    ) -> Self:
        self = super().__new__(cls)
        self._synthesis = synthesis
        self._playback = playback
        self._music = music
        self._chime_dedup = chime_dedup
        self._once_dedup = once_dedup
        self._chimes = chimes
        self._health = health
        self._auth_token = auth_token
        self._client_count: int = 0
        self._handlers: dict[str, Callable] = {
            "synthesize": self._handle_synthesize,
            "chime": self._handle_chime,
            "record": self._handle_record,
            "voices": self._handle_voices,
            "health": self._handle_health,
            "music_on": self._handle_music_on,
            "music_off": self._handle_music_off,
            "music_play": self._handle_music_play,
            "music_list": self._handle_music_list,
            "music_vibe": self._handle_music_vibe,
            "music_next": self._handle_music_next,
        }
        return self

    @property
    def client_count(self) -> int: return self._client_count

    async def handle_connection(self, websocket: WebSocket) -> None: ...

```

**Functions that become methods** (from `_monolith.py`):

- `_ws_route(websocket)` -> `WebSocketRouter.handle_connection(websocket)`
- `_check_auth(websocket, ctx)` -> `WebSocketRouter._check_auth(websocket)`
- `_handle_synthesize(msg, ws, ctx)` -> `WebSocketRouter._handle_synthesize(msg, ws)`
- `_handle_record(msg, ws, ctx)` -> `WebSocketRouter._handle_record(msg, ws)`
- `_handle_chime(msg, ws, ctx)` -> `WebSocketRouter._handle_chime(msg, ws)`
- `_handle_voices(msg, ws, ctx)` -> `WebSocketRouter._handle_voices(msg, ws)`
- `_handle_health(msg, ws, ctx)` -> `WebSocketRouter._handle_health(msg, ws)`
- `_handle_music_on(msg, ws, ctx)` -> `WebSocketRouter._handle_music_on(msg, ws)` (delegates to `self._music.turn_on(...)`)
- `_handle_music_off(msg, ws, ctx)` -> `WebSocketRouter._handle_music_off(msg, ws)` (delegates to `self._music.turn_off()`)
- `_handle_music_play(msg, ws, ctx)` -> similar delegation
- `_handle_music_list(msg, ws, ctx)` -> delegates to `self._music._generator.list_tracks()`
- `_handle_music_vibe(msg, ws, ctx)` -> delegates to `self._music.update_vibe(...)`
- `_handle_music_next(msg, ws, ctx)` -> delegates to `self._music.skip_next(...)`
- `_parse_optional_float`, `_parse_optional_int`, `_parse_optional_str` -> staticmethods on `WebSocketRouter`

**Constants that move**: `_HANDLERS` dict (becomes instance attribute)

**State that moves off DaemonContext**:

- `auth_token` -> `WebSocketRouter._auth_token` (and `DaemonConfig._auth_token`)
- `client_count` -> `WebSocketRouter._client_count`
- `chime_dedup` -> `WebSocketRouter._chime_dedup` (ref)
- `once_dedup` -> `WebSocketRouter._once_dedup` (ref)

**DaemonContext delegations**: All existing delegations on DaemonContext for music fields are removed. The music handlers in the router call `self._music.turn_on(...)` etc. directly.

**Music handler transformation**: The current music handlers like `_handle_music_on(msg, ws, ctx)` do two things: (1) parse the wire-protocol message and (2) mutate `ctx.music_*` state. In the new design, the router handler parses the message and calls `self._music.turn_on(owner_id, style, vibe, vibe_tags, name)`, which returns a response dict. The router sends it over the websocket. This separates protocol parsing from domain logic.

**Tests that move**: `TestHandleSynthesizeShortCircuit`, `TestHandleSynthesizeOnceFlag`, `TestWsRoutePeerClose`, `TestHandlerRegistration`, `TestHandleMusicOn`, `TestHandleMusicOff`, `TestHandleMusicVibe`, `TestHandleMusicOnWhilePlaying`, `TestHandleMusicNext`, `TestHandleMusicPlay`, `TestHandleMusicList`, `TestHandleMusicOnWithName`, `TestEmptyOwnerIdRejection` move to `tests/test_voxd_router.py`.

These tests now construct a `WebSocketRouter` with injected fakes for all dependencies. Tests that called `_handle_music_on(msg, ws, ctx)` now call `router._handle_music_on(msg, ws)` and verify router interactions with mock domain objects.

**Mock target changes**:

- `"punt_vox.voxd._monolith.auto_detect_provider"` -> `"punt_vox.voxd.router.auto_detect_provider"`
- `"punt_vox.voxd._monolith._try_direct_play"` -> no longer needed (router calls `self._synthesis.try_direct_play()`)
- `"punt_vox.voxd._monolith._synthesize_to_file"` -> no longer needed (router calls `self._synthesis.synthesize_to_file()`)
- `"punt_vox.voxd._monolith._music_output_dir"` -> no longer needed (music handlers delegate to `self._music`)

**Verification**: `make check`

---

### Step 10: Create the `VoxDaemon` class and eliminate `DaemonContext`

**Class being created**: `VoxDaemon`
**Module**: `src/punt_vox/voxd/daemon.py`

**Constructor**:

```python
class VoxDaemon:
    def __new__(
        cls,
        config: DaemonConfig,
        playback: PlaybackQueue,
        synthesis: SynthesisPipeline,
        music: MusicScheduler,
        health: DaemonHealth,
        router: WebSocketRouter,
    ) -> Self:
        self = super().__new__(cls)
        self._config = config
        self._playback = playback
        self._synthesis = synthesis
        self._music = music
        self._health = health
        self._router = router
        return self

    def build_app(self) -> Starlette: ...
    def run(self, host: str, port: int) -> None: ...

```

**What happens to DaemonContext**: Deleted entirely. By this step, all fields have migrated:

- `auth_token` -> `DaemonConfig` / `WebSocketRouter`
- `port` -> `DaemonHealth` / `DaemonConfig`
- `start_time` -> `DaemonHealth`
- `daemon_version` -> `DaemonHealth`
- `client_count` -> `WebSocketRouter`
- `playback_queue` -> `PlaybackQueue`
- `last_playback` -> `PlaybackQueue`
- `chime_dedup` -> `WebSocketRouter` (holds ref)
- `once_dedup` -> `WebSocketRouter` (holds ref)
- `music_*` (10 fields) -> `MusicScheduler`

**Functions that become methods** (from `_monolith.py`):

- `build_app(ctx)` -> `VoxDaemon.build_app()`
- `main(port, host)` -> `VoxDaemon.run(host, port)` (the composition logic that wires everything)
- `_health_route(request)` -> `VoxDaemon._health_route(request)` (delegates to `self._health.minimal_payload()`)
- The lifespan function becomes a method

__What remains in `_monolith.py`**: Only `cli`, `entrypoint`, and `if **name** == "**main__"`. These are the CLI entry points that construct a `VoxDaemon` and call `run()`.

**Rename**: `_monolith.py` is renamed to `daemon.py`. This is the final module name. Update `__init__.py` to import from `daemon` instead of `_monolith`.

**Mock target changes**: All remaining `"punt_vox.voxd._monolith.X"` become `"punt_vox.voxd.daemon.X"`. At this point there should be very few -- most functionality has already migrated to named submodules.

**Tests that move**: The `TestApiKeyPassthroughIntegration` and `TestCacheApiKeyBypass` tests that use `build_app(ctx)` now use `VoxDaemon.build_app()` or construct the app via the daemon's composition logic.

**Test cleanup**: `test_voxd.py` should now be empty or nearly empty. Any remaining tests move to `test_voxd_daemon.py`. Delete `test_voxd.py` if empty.

**Verification**: `make check`

---

### Step 11 (service.py): Create the `ProcessManager` class

**Class being created**: `ProcessManager`
**Module**: `src/punt_vox/service/process.py`

Phase 5 begins. This is independent of the voxd work.

**Package creation** (simultaneous):

1. Create `src/punt_vox/service/` directory.
2. Move `src/punt_vox/service.py` to `src/punt_vox/service/_monolith.py`.
3. Create `src/punt_vox/service/__init__.py` with re-exports.
4. Create `src/punt_vox/service/process.py` with the `ProcessManager` class.

**Constructor**:

```python
class ProcessManager:
    def __new__(cls, default_port: int = DEFAULT_PORT) -> Self:
        self = super().__new__(cls)
        self._default_port = default_port
        return self

```

**Functions that become methods**: `_find_pid_on_port`, `_is_vox_daemon_process`, `_kill_pid`, `_kill_stale_daemon`, `_ensure_port_free`, `read_port_file`, `_remove_port_file`

**Constants that move**: `_run_dir`, `_KILL_TIMEOUT_SECONDS`, `_SUBPROCESS_TIMEOUT_SECONDS`

**Mock target changes**: ALL `patch("punt_vox.service.X", ...)` in `test_service.py` become `patch("punt_vox.service._monolith.X", ...)` for the package conversion, then process-related ones become `patch("punt_vox.service.process.X", ...)`.

**Tests that move**: Process-related tests from `test_service.py` move to `test_service_process.py`.

**Verification**: `make check`

---

### Step 12: Create the `KeysEnvWriter` class

**Class being created**: `KeysEnvWriter`
**Module**: `src/punt_vox/service/keys_env.py`

**Constructor**:

```python
class KeysEnvWriter:
    def __new__(cls, keys_path: Path) -> Self:
        self = super().__new__(cls)
        self._keys_path = keys_path
        return self

```

**Functions that become methods**: `_write_keys_env` -> `KeysEnvWriter.write(env) -> Path`

**Uses**: `from punt_vox.keys import PROVIDER_KEY_NAMES`

**Tests that move**: Keys-env related tests to `test_service_keys_env.py`.

**Verification**: `make check`

---

### Step 13: Create the `LaunchdBackend` class

**Class being created**: `LaunchdBackend`
**Module**: `src/punt_vox/service/launchd.py`

**Constructor**:

```python
class LaunchdBackend:
    def __new__(cls, label: str = _LABEL, plist_dir: Path = _LAUNCHD_DIR) -> Self:
        self = super().__new__(cls)
        self._label = label
        self._plist_path = plist_dir / _LAUNCHD_PLIST
        return self

```

**Functions that become methods**: `_launchd_plist_content`, `_launchd_stop`, `_launchd_install`, `_launchd_uninstall`, `_launchd_status`, `_extra_launchd_env`

**Constants that move**: `_LAUNCHD_DIR`, `_LAUNCHD_PLIST`, `_LABEL`

**Tests that move**: Launchd-related tests to `test_service_launchd.py`.

**Verification**: `make check`

---

### Step 14: Create the `SystemdBackend` class

**Class being created**: `SystemdBackend`
**Module**: `src/punt_vox/service/systemd.py`

**Constructor**:

```python
class SystemdBackend:
    def __new__(cls, unit_dir: Path = _SYSTEMD_DIR) -> Self:
        self = super().__new__(cls)
        self._unit_path = unit_dir / _SYSTEMD_UNIT
        return self

```

**Functions that become methods**: `_systemd_unit_content`, `_systemd_stop`, `_systemd_install`, `_systemd_uninstall`, `_systemd_status`, `_systemd_audio_env_lines`, `_safe_systemd_value`, `_cleanup_stale_user_unit`, `_legacy_user_unit_path`

**Constants that move**: `_SYSTEMD_DIR`, `_SYSTEMD_UNIT`, `_LEGACY_USER_UNIT_RELATIVE`

**Tests that move**: Systemd-related tests to `test_service_systemd.py`.

**Verification**: `make check`

---

### Step 15: Create `ServiceInstaller` and eliminate `service/_monolith.py`

**Class being created**: `ServiceInstaller`
**Module**: `src/punt_vox/service/installer.py`

**Constructor**:

```python
class ServiceInstaller:
    def __new__(
        cls,
        *,
        process_manager: ProcessManager,
        keys_writer: KeysEnvWriter,
        launchd: LaunchdBackend,
        systemd: SystemdBackend,
    ) -> Self:
        self = super().__new__(cls)
        self._process_manager = process_manager
        self._keys_writer = keys_writer
        self._launchd = launchd
        self._systemd = systemd
        return self

```

**Functions that become methods**: `install`, `uninstall`, `is_running`, `detect_platform`, `_ensure_user_dirs`, `_voxd_exec_args`

**Constants that move**: `DEFAULT_PORT`, `_SUDO_NOTICE`

**max_complexity fix**: The function with CC=20 (likely `install`) must have
Extract Method applied when it becomes `ServiceInstaller.install()`. Extract
platform-specific branches into `_install_darwin()` and `_install_linux()`
private methods, and extract key-writing logic into a `_write_keys(env)`
helper. Target: no method exceeds CC=10.

**What remains in `_monolith.py`**: Nothing. Rename to `installer.py` or delete if already empty.

**Update `**init**.py`**: All imports now come from the named submodules.

**Tests that move**: Remaining tests from `test_service.py` to `test_service_installer.py`. Delete `test_service.py` if empty.

**Verification**: `make check`

---

### Step 16: Refactor `SessionState` to `SessionConfig` in `server.py`

**What changes** (in-place refactor):

1. Rename `SessionState` to `SessionConfig`.
2. Move `_refresh_state_from_config` into `SessionConfig.refresh_from_config()` method.
3. Move `_seed_state_from_config` into `SessionConfig.from_config(config_dir)` classmethod.
4. The module-level `_state` singleton becomes `_session: SessionConfig`, initialized in `run_server()`. The important change: refresh logic is a method, not an external function.
5. Update all MCP tool functions to call `_session.refresh_from_config()`.
6. `_speak_explicit` boolean becomes an attribute on `SessionConfig`.

**Tests that move**: Tests in `test_server.py` that reference `SessionState` update to `SessionConfig`. Mock targets for `_refresh_state_from_config` become `SessionConfig.refresh_from_config` or a wrapper.

**Verification**: `make check`

---

### Step 17: Extract `DoctorCheck` from `__main__.py`

**Class being created**: `DoctorCheck`
**Module**: `src/punt_vox/doctor.py`

**Constructor**:

```python
@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    message: str
    detail: str = ""

class DoctorCheck:
    def __new__(cls, client: VoxClientSync | None = None) -> Self:
        self = super().__new__(cls)
        self._client = client
        return self

    def run_all(self) -> list[CheckResult]: ...
    def check_daemon_running(self) -> CheckResult: ...
    def check_version_match(self) -> CheckResult: ...
    def check_provider_available(self) -> CheckResult: ...
    # ... one method per diagnostic check

```

**What moves**: The ~300 lines of diagnostic logic from `__main__.py`'s `doctor` command.

**What stays in `**main**.py`**: The `doctor` command becomes a thin wrapper:

```python
@app.command()
def doctor():
    check = DoctorCheck(client=...)
    results = check.run_all()
    for r in results:
        # format and print

```

**Tests**: New `tests/test_doctor.py` for the extracted logic. Existing `test_cli.py` tests for `doctor` continue working (they test via CLI invocation).

**Verification**: `make check`

---

## Part 3: Verification Checklist

There is no separate "test migration" phase. Tests move in the same step as the code they test.

After all steps complete:

```bash
make check                          # all tests pass
wc -l src/punt_vox/voxd/*.py       # no file > 500 lines
wc -l src/punt_vox/service/*.py    # no file > 500 lines
wc -l src/punt_vox/server*.py       # each < 300 lines (after Step 46)
wc -l src/punt_vox/doctor.py       # ~ 300 lines
wc -l src/punt_vox/cli_*.py        # each < 300 lines (after Steps 47-51)
wc -l src/punt_vox/__main__.py     # ~ 300 lines (after Steps 47-51)

```

**Expected file counts**:

| File | Target LOC |
|------|-----------|
| `voxd/__init__.py` | ~50 (re-exports only) |
| `voxd/daemon.py` | ~120 |
| `voxd/playback.py` | ~200 |
| `voxd/synthesis.py` | ~350 |
| `voxd/music_scheduler.py` | ~300 |
| `voxd/track_generator.py` | ~150 |
| `voxd/dedup.py` | ~130 |
| `voxd/chimes.py` | ~40 |
| `voxd/router.py` | ~400 |
| `voxd/health.py` | ~60 |
| `voxd/config.py` | ~200 |
| `service/__init__.py` | ~30 |
| `service/installer.py` | ~200 |
| `service/keys_env.py` | ~100 |
| `service/process.py` | ~150 |
| `service/launchd.py` | ~150 |
| `service/systemd.py` | ~200 |
| `server.py` | ~250 (after Step 46 split) |
| `server_speech.py` | ~200 (Step 46) |
| `server_music.py` | ~150 (Step 46) |
| `server_config.py` | ~150 (Step 46) |
| `doctor.py` | ~300 |
| `cli_types.py` | ~80 (Step 47) |
| `cli_daemon.py` | ~300 (Step 48) |
| `cli_music.py` | ~250 (Step 49) |
| `cli_cache.py` | ~150 (Step 50) |
| `__main__.py` | ~300 (after Steps 47-51) |

No module exceeds 300 lines after all steps complete.

---

## Risks and Mitigations

### R1: Async boundary

`DaemonContext` fields are mutated from both WebSocket handler coroutines
and background tasks. The current code is safe because everything runs on
one event loop thread. The refactoring must preserve this: no new
`asyncio.Lock` where none exists today, no thread spawning that mutates
shared state. The only locks that exist in the final design are `_env_lock`
(owned by SynthesisPipeline) and `_playback_mutex` (owned by PlaybackQueue).
Both exist today.

### R2: Mock target avalanche

The module-to-package conversion changes every mock target in
`test_voxd.py`. Mitigation: this happens in Step 1 as part of a single
atomic commit that also creates the first real class (DaemonConfig). Having
the mock targets stabilized early prevents compounding changes.

### R3: DaemonContext delegation complexity

Each step adds delegation properties to DaemonContext so old code (handlers
and tests that haven't moved yet) continues to work through the familiar
`ctx.music_mode` interface. The risk is that the delegation layer becomes
the permanent design. Mitigation: the delegation properties are removed in
the same step that moves the handlers to WebSocketRouter (Step 9). After
Step 10, DaemonContext is deleted entirely.

### R4: Import cycles

The dependency graph is acyclic by design. The only risk is if
`music_scheduler.py` and `playback.py` develop a bidirectional
dependency. Mitigation: music uses its own subprocess-based playback
(not the queue), so no backward dependency exists.

### R5: Integration tests that use `build_app`

`TestApiKeyPassthroughIntegration` and `TestCacheApiKeyBypass` drive the
full Starlette app via TestClient. These tests depend on the exact wiring
in `build_app`. Mitigation: the `VoxDaemon.build_app()` method in Step 10
preserves the same route structure and handler dispatch. The tests are
updated to construct a VoxDaemon instead of calling `build_app(ctx)` directly.

---

## Execution Summary

| Phase | Steps | Risk |
|-------|-------|------|
| 0: Shared constants | 1 | Low |
| 1: Package + DaemonConfig | 1 | HIGH (mock target migration) |
| 2: ChimeResolver | 1 | Low |
| 3: Dedup classes | 1 | Low |
| 4: PlaybackQueue | 1 | Medium (ctx delegation) |
| 5: TrackGenerator | 1 | Low |
| 6: SynthesisPipeline | 1 | Medium (cache injection, ctx elimination) |
| 7: MusicScheduler | 1 | HIGH (10 fields off DaemonContext) |
| 8: DaemonHealth | 1 | Low |
| 9: WebSocketRouter | 1 | HIGH (all handlers move, ctx delegations removed) |
| 10: VoxDaemon + DaemonContext deletion | 1 | Medium |
| 11-15: service.py decomposition | 5 | Medium (same pattern as voxd) |
| 16: SessionConfig | 1 | Low |
| 17: DoctorCheck | 1 | Low |
| **Total Part 2** | **18** | |

Each step produces a finished class with constructor injection, owned state,
and public methods. Each step passes `make check`. DaemonContext shrinks
at steps 4, 7, 8, and 9, and is deleted at step 10. An implementer with no
prior context can execute these steps by following the class specification,
constructor parameters, method list, state migration, delegation additions,
mock target changes, and test file movements for each step.

---

## Part 3: Codebase-Wide OO Compliance

### 3.0 Threshold Reconciliation

The `tools/oo_score.py` enforces `module_size <= 300`. Part 2 targets `<= 500`
for extracted modules. These two numbers must agree.

The tool is the gate -- its thresholds are the source of truth. The Part 2
target of 500 was calibrated before the tool existed. The corrected target
is **<= 300 LOC** for all library modules, with these consequences:

- `voxd/router.py` (~400 target in Part 2) must be split further. Extract
  music-related handlers into `voxd/music_handlers.py` (~150 LOC), leaving
  `router.py` at ~250 LOC.
- `voxd/synthesis.py` (~350 target in Part 2) must split. Extract pure
  functions (`_build_audio_request`, vibe helpers) into `voxd/synthesis_support.py`
  (~100 LOC), leaving `synthesis.py` at ~250 LOC.
- `voxd/music_scheduler.py` (~300 target in Part 2) is at the boundary -- acceptable.
- `server.py` (~900 currently) is the MCP tool surface. Each MCP tool is a
  separate function registered with FastMCP -- the file is inherently flat.
  Split into `server.py` (session config + tool registration, ~250 LOC) and
  `server_tools.py` (tool implementations, grouped by domain, split as needed
  to stay under 300).
- `doctor.py` (~300 target) is at the boundary -- acceptable.

The `Calibration` table in Part 1 is also corrected:

| Metric | merchants/game | punt-vox (current) | punt-vox (target) |
|--------|---------------|--------------------|--------------------|
| Max module LOC | 363 (captain.py) | 2729 (voxd.py) | <=300 |

Part 2 step targets that exceed 300 LOC must be split as part of that step.
This is a refinement, not a contradiction -- the class specifications remain
the same; the module boundaries tighten.

### 3.1 How to Read Each Step Below

Each step follows a fixed format:

- **File**: the source module being changed
- **Failures**: which `oo_score.py` metrics currently fail
- **Class**: the class to create (name, module location)
- **Constructor**: `__new__` or `@dataclass(frozen=True, slots=True)` signature
- **Methods**: which module-level functions become methods
- **init_violations fix**: what changes (`__init__` -> `__new__`, or -> `@dataclass`)
- **Verification**: `make check` including `make check-oo`

Steps are grouped by tier. Within a tier, steps are independent and can be
executed in any order. Each step must leave `make check` passing.

---

### Tier 2: Significant Refactoring

#### Step 18: Wrap `hooks.py` in a `HookDispatcher` class

**File**: `src/punt_vox/hooks.py` (501 LOC, 4 failures)
**Failures**: `method_ratio=0.0`, `class_to_func_ratio=0.0`, `module_size=501`, `avg_complexity=5.72`

**Class**: `HookDispatcher`
**Module**: `src/punt_vox/hooks.py`

**Why one class**: Every function in this module reads hook input, resolves
config, calls voxd via `VoxClientSync`, and emits output. They share the same
collaborators: a `VoxClientSync` factory, a config directory, and an emit
function. The dispatch functions are methods on an object that owns these
collaborators.

**Constructor**:

```python
class HookDispatcher:
    def __new__(cls, *, config_dir: Path | None = None) -> Self:
        self = super().__new__(cls)
        self._config_dir = config_dir
        return self
```

**Methods** (module-level functions that become methods):

- `handle_stop(data, config)` -> `HookDispatcher.handle_stop(data, config)`
- `handle_post_bash(data, config_dir)` -> `HookDispatcher.handle_post_bash(data)`
- `handle_notification(data, config)` -> `HookDispatcher.handle_notification(data, config)`
- `handle_pre_compact(config)` -> `HookDispatcher.handle_pre_compact(config)`
- `handle_user_prompt_submit(config)` -> `HookDispatcher.handle_user_prompt_submit(config)`
- `handle_subagent_start(config)` -> `HookDispatcher.handle_subagent_start(config)`
- `handle_subagent_stop(config)` -> `HookDispatcher.handle_subagent_stop(config)`
- `handle_session_end(config, config_dir)` -> `HookDispatcher.handle_session_end(config)`
- `classify_signal(exit_code, stdout)` -> `HookDispatcher.classify_signal(exit_code, stdout)` (staticmethod)
- `resolve_tags_from_signals(signals)` -> `HookDispatcher.resolve_tags_from_signals(signals)` (staticmethod)
- `_speak_via_voxd(...)` -> `HookDispatcher._speak_via_voxd(...)`
- `_chime_via_voxd(signal)` -> `HookDispatcher._chime_via_voxd(signal)`
- `_make_client()` -> `HookDispatcher._make_client()`
- `_read_hook_input()` -> stays module-level (pure I/O, no self)
- `_emit(output)` -> stays module-level (pure I/O, no self)
- `_pick_notification_phrase(...)` -> `HookDispatcher._pick_notification_phrase(...)` (staticmethod)
- `_speak_phrase(...)` -> `HookDispatcher._speak_phrase(...)`

The Typer CLI subcommands (`stop_cmd`, `post_bash_cmd`, etc.) stay as
module-level functions -- they are the CLI entrypoint layer, not domain logic.
They construct a `HookDispatcher` and delegate.

**max_complexity fix**: The function with CC=21 (likely `handle_stop` or
`handle_post_bash`) must have Extract Method applied. Identify the branches
(signal classification, config lookup, client dispatch) and extract each
branch into a private method. Target: no method exceeds CC=10.

**Module size**: The file is 501 LOC. After wrapping into a class, LOC stays
roughly the same. Split the Typer CLI commands into a separate file
(`hooks_cli.py`, ~100 LOC) to bring `hooks.py` under 300.

**init_violations fix**: N/A (no `__init__` exists currently)

**Verification**: `make check` including `make check-oo`

---

#### Step 19: Wrap `normalize.py` in a `TextNormalizer` class

**File**: `src/punt_vox/normalize.py` (654 LOC, 5 failures)
**Failures**: `method_ratio=0.0`, `class_to_func_ratio=0.0`, `module_size=654`, `max_complexity=18`, `avg_complexity=7.43`

**Class**: `TextNormalizer`
**Module**: `src/punt_vox/normalize.py`

**Why one class**: The module has large data tables (abbreviation dicts, regex
patterns) and 7 functions that operate on those tables. The tables are the
state; the functions are the behavior. A `TextNormalizer` class owns the tables
and provides the normalization methods.

**Constructor**:

```python
class TextNormalizer:
    def __new__(cls) -> Self:
        self = super().__new__(cls)
        # Tables are class-level constants, no instance state needed
        return self
```

**Methods**:

- `normalize_for_speech(text)` -> `TextNormalizer.normalize(text)`
- `strip_vibe_tags(text)` -> `TextNormalizer.strip_vibe_tags(text)`
- `_normalize_token(token)` -> `TextNormalizer._normalize_token(token)`
- `_expand_part(part)` -> `TextNormalizer._expand_part(part)`
- `_space_acronym(part)` -> `TextNormalizer._space_acronym(part)` (staticmethod)
- `_expand_abbreviation(word)` -> `TextNormalizer._expand_abbreviation(word)`
- `_strip_punctuation(token)` -> `TextNormalizer._strip_punctuation(token)` (staticmethod)

All data tables (`_ABBREVIATIONS`, `_SNAKE_RE`, etc.) become class-level
constants on `TextNormalizer`.

**Module backward compat**: Add a module-level convenience function
`normalize_for_speech = TextNormalizer().normalize` so callers don't need to
change. Remove once all callers are updated.

**Module size**: 654 LOC is mostly data tables (~530 lines of dict literals).
Do NOT split the tables into a separate `normalize_tables.py` -- a pure-data
module with ~400 LOC and many top-level dict assignments will fail
`module_size`, `method_ratio`, and `class_to_func_ratio` (the tool reports
failures when `top_stmts > 5` and there are no classes or methods). Instead,
keep the data tables as **class-level constants on `TextNormalizer`**:

```python
class TextNormalizer:
    _ABBREVIATIONS: ClassVar[dict[str, str]] = { ... }
    _SNAKE_RE: ClassVar[re.Pattern[str]] = re.compile(...)
    # ... all other tables as ClassVar
```

This keeps all data co-located with the methods that use it. The single file
will be ~654 LOC, which exceeds `module_size <= 300`. Split instead by
extracting the class into two classes in separate files:

- `normalize.py` (~250 LOC): `TextNormalizer` class with methods and small
  tables (regexes, short mappings)
- `normalize_abbreviations.py` (~400 LOC): `AbbreviationExpander` class that
  owns `_ABBREVIATIONS` dict as a `ClassVar` and exposes
  `expand(word) -> str` as a staticmethod. `TextNormalizer` delegates to
  `AbbreviationExpander.expand()`.

Both files have a class, pass `method_ratio` and `class_to_func_ratio`, and
the large dict is a class constant (not a top-level statement).

**init_violations fix**: N/A (no `__init__` exists currently)

**Verification**: `make check` including `make check-oo`

---

#### Step 20: Move `watcher.py` module functions into `SessionWatcher` and companions

**File**: `src/punt_vox/watcher.py` (384 LOC, 5 failures)
**Failures**: `method_ratio=0.375`, `class_to_func_ratio=0.167`, `module_size=384`, `max_complexity=12`, `init_violations=1`

**Classes**:

1. `SessionWatcher` (exists) -- absorbs the remaining module-level functions
2. `ChimeResolver` (new, separate from the voxd `ChimeResolver`) -- wraps
   `resolve_chime_path`, `_resolve_assets_dir`, `_announce_chime`
3. `SessionEvent` (exists, already a dataclass) -- no changes needed

**SessionWatcher changes**:

- `classify_output(text)` -> `SessionWatcher.classify_output(text)` (staticmethod)
- `_extract_tool_result_text(data)` -> `SessionWatcher._extract_tool_result_text(data)` (staticmethod)
- `_content_to_text(content)` -> `SessionWatcher._content_to_text(content)` (staticmethod)
- `derive_session_dir(cwd)` -> `SessionWatcher.derive_session_dir(cwd)` (staticmethod)
- `_find_session_jsonl(session_dir)` -> `SessionWatcher._find_session_jsonl(session_dir)` (staticmethod)
- `make_notification_consumer(...)` -> `SessionWatcher.make_notification_consumer(...)`

**New ChimeResolver class** (in same file or separate `watcher_chimes.py`):

```python
class ChimeResolver:
    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._assets_dir: Path | None = cls._resolve_assets_dir()
        return self

    def resolve_chime_path(self, signal: str, vibe: str | None = None) -> Path | None: ...
    def announce(self, signal: str, vibe: str | None = None) -> None: ...

    @staticmethod
    def _resolve_assets_dir() -> Path | None: ...
```

- `resolve_chime_path(...)` -> `ChimeResolver.resolve_chime_path(...)`
- `_resolve_assets_dir()` -> `ChimeResolver._resolve_assets_dir()` (staticmethod)
- `_announce_chime(signal, vibe)` -> `ChimeResolver.announce(signal, vibe)`
- `_announce_voice(event)` stays on `SessionWatcher` (it uses watcher state)

**init_violations fix**: `SessionWatcher.__init__` -> `SessionWatcher.__new__`

**Module size**: If both classes stay in one file, ~384 LOC (at boundary).
Split `ChimeResolver` into `watcher_chimes.py` (~80 LOC) to bring the main
file under 300.

**Verification**: `make check` including `make check-oo`

---

#### Step 21: Fix `client.py` -- init_violations, class count, complexity

**File**: `src/punt_vox/client.py` (623 LOC, 5 failures)
**Failures**: `module_size=623`, `classes_per_module=5`, `class_to_func_ratio=0.455`, `max_complexity=13`, `init_violations=2`

**Changes**:

1. **init_violations**: `VoxClient.__init__` and `VoxClientSync.__init__` both
   become `__new__` methods. The constructors set connection parameters and
   create internal state -- straightforward conversion.

2. **Module-level functions become staticmethods on `VoxClient`**:
   - `_env_host()` -> `VoxClient._env_host()` (staticmethod)
   - `_env_port()` -> `VoxClient._env_port()` (staticmethod)
   - `_env_token()` -> `VoxClient._env_token()` (staticmethod)
   - `_run_dir()` -> `VoxClient._run_dir()` (staticmethod)
   - `read_port_file()` -> `VoxClient.read_port_file()` (classmethod, public)
   - `read_token_file()` -> `VoxClient.read_token_file()` (classmethod, public)

3. **Module size / class count**: Split into two files:
   - `client.py` (~350 LOC): `VoxClient`, `SynthesizeResult` -- the async client
   - `client_sync.py` (~200 LOC): `VoxClientSync` -- the sync wrapper
   - Move `VoxdConnectionError` and `VoxdProtocolError` to `types.py` (they are
     domain types used by callers)

   This brings both files under 300 LOC and reduces `classes_per_module` from 5
   to 2-3.

4. **Complexity**: The method with CC=13 needs `Extract Method` to bring it
   under 10.

**init_violations fix**: `VoxClient.__init__` -> `VoxClient.__new__`;
`VoxClientSync.__init__` -> `VoxClientSync.__new__`

**Verification**: `make check` including `make check-oo`

---

#### Step 22: Fix `types.py` -- encapsulation, public_attr_violations, init_violations

**File**: `src/punt_vox/types.py` (278 LOC, 5 failures)
**Failures**: `classes_per_module=8`, `method_ratio=0.778`, `encapsulation_ratio=0.0`, `init_violations=1`, `public_attr_violations=2`

**Changes**:

1. **public_attr_violations / encapsulation**: The `VoiceNotFoundError.__init__`
   stores `self.name` and `self.available` as public attributes. Fix: prefix
   with underscore, add `@property` getters:

   ```python
   self._name = name
   self._available = available

   @property
   def name(self) -> str: return self._name
   @property
   def available(self) -> list[str]: return list(self._available)
   ```

2. **init_violations**: `VoiceNotFoundError.__init__` -> `VoiceNotFoundError.__new__`.
   Since this extends `ValueError`, construction uses `__new__` with
   `super().__new__(cls, message)`.

3. **method_ratio**: Two module-level functions need to become methods:
   - `validate_language(code)` -> staticmethod on a class, or method on a
     small `LanguageValidator` class. Since it's used by providers, make it a
     staticmethod on `AudioRequest` or a standalone `Validation` class.
   - `result_to_dict(result)` -> `AudioResult.to_dict()` (method on `AudioResult`)
   - `generate_filename(text, prefix)` -> staticmethod on `AudioRequest`
   - `_metadata()` -> classmethod on `AudioProviderId` or delete if unused externally

4. **classes_per_module=8**: The file has 8 non-Protocol classes. The tool
   threshold is <=3. Split into:
   - `types.py` (~150 LOC): `AudioProviderId`, `AudioRequest`, `AudioResult`,
     `MergeStrategy`, `HealthCheck` (core value types)
   - `protocols.py` (~100 LOC): `AudioProvider`, `TTSProvider`,
     `DirectPlayProvider`, `MusicProvider`, `MusicRequest`, `MusicResult`
     (Protocol/interface types -- the tool excludes Protocol from class count)
   - `errors.py` (~30 LOC): `VoiceNotFoundError` (plus client errors from Step 21)

**init_violations fix**: `VoiceNotFoundError.__init__` -> `VoiceNotFoundError.__new__`

**Verification**: `make check` including `make check-oo`

---

### Tier 3: Provider Classes

All six provider files have `init_violations` (using `__init__` instead of
`__new__`). Some also have `class_to_func_ratio` failures from module-level
helper functions. The fix pattern is the same for each.

#### Step 23: Fix `providers/elevenlabs.py` -- init_violations, class_to_func_ratio

**File**: `src/punt_vox/providers/elevenlabs.py` (502 LOC, 4 failures)
**Failures**: `module_size=502`, `class_to_func_ratio=0.333`, `init_violations=1`, `avg_complexity=5.83`

**Changes**:

1. **init_violations**: `ElevenLabsProvider.__init__` -> `ElevenLabsProvider.__new__`
2. **Module functions become methods**:
   - `_load_voices_from_api(client)` -> `ElevenLabsProvider._load_voices_from_api()`
     (uses `self._client`)
   - `_extract_api_error_message(exc)` -> `ElevenLabsProvider._extract_api_error_message(exc)`
     (staticmethod)
3. **module_size**: Split voice loading cache logic into
   `providers/elevenlabs_voices.py` (~100 LOC) to bring main file under 300.

**init_violations fix**: `ElevenLabsProvider.__init__` -> `ElevenLabsProvider.__new__`

**Verification**: `make check` including `make check-oo`

---

#### Step 24: Fix `providers/polly.py` -- init_violations, class_to_func_ratio

**File**: `src/punt_vox/providers/polly.py` (399 LOC, 4 failures)
**Failures**: `module_size=399`, `class_to_func_ratio=0.333`, `init_violations=1`, `avg_complexity=5.06`

**Changes**:

1. **init_violations**: `PollyProvider.__init__` -> `PollyProvider.__new__`
2. **Module functions become methods or staticmethods on PollyProvider**:
   - `_bcp47_matches_iso(bcp47, iso)` -> `PollyProvider._bcp47_matches_iso()` (staticmethod)
   - `_infer_iso_from_bcp47(bcp47)` -> `PollyProvider._infer_iso_from_bcp47()` (staticmethod)
   - `_best_engine(supported)` -> `PollyProvider._best_engine()` (staticmethod)
   - `_load_voices_from_api(client)` -> `PollyProvider._load_voices_from_api()` (uses `self._client`)
3. **module_size**: Split `VoiceConfig` and voice-loading logic into
   `providers/polly_voices.py` (~120 LOC) to bring main file under 300.

**init_violations fix**: `PollyProvider.__init__` -> `PollyProvider.__new__`

**Verification**: `make check` including `make check-oo`

---

#### Step 25: Fix `providers/openai.py` -- init_violations

**File**: `src/punt_vox/providers/openai.py` (244 LOC, 1 failure)
**Failures**: `init_violations=1`

**Changes**:

1. **init_violations**: `OpenAIProvider.__init__` -> `OpenAIProvider.__new__`

The file is under 300 LOC, has no module-level functions, and passes all other
metrics. This is a single-line constructor conversion.

**init_violations fix**: `OpenAIProvider.__init__` -> `OpenAIProvider.__new__`

**Verification**: `make check` including `make check-oo`

---

#### Step 26: Fix `providers/say.py` -- init_violations, class_to_func_ratio, module_size

**File**: `src/punt_vox/providers/say.py` (330 LOC, 3 failures)
**Failures**: `module_size=330`, `class_to_func_ratio=0.4`, `init_violations=1`

**Changes**:

1. **init_violations**: `SayProvider.__init__` -> `SayProvider.__new__`
2. **Module functions become methods or staticmethods**:
   - `_locale_to_iso(locale)` -> `SayProvider._locale_to_iso()` (staticmethod)
   - `_load_voices_from_system()` -> `SayProvider._load_voices()` (classmethod, populates class-level cache)
   - `_rate_to_wpm(rate)` -> `SayProvider._rate_to_wpm()` (staticmethod)
3. **module_size**: `SayVoiceConfig` dataclass can stay in the same file.
   Bringing functions inside the class reduces cognitive load but not LOC.
   Target is borderline (330 -> ~300 after removing standalone `def` lines).
   If still over 300, move `SayVoiceConfig` to `providers/say_voices.py`.

**init_violations fix**: `SayProvider.__init__` -> `SayProvider.__new__`

**Verification**: `make check` including `make check-oo`

---

#### Step 27: Fix `providers/espeak.py` -- init_violations, class_to_func_ratio

**File**: `src/punt_vox/providers/espeak.py` (289 LOC, 3 failures)
**Failures**: `class_to_func_ratio=0.333`, `init_violations=1`, `max_complexity=14`

**Changes**:

1. **init_violations**: `EspeakProvider.__init__` -> `EspeakProvider.__new__`
2. **Module functions become methods or staticmethods**:
   - `_find_espeak_binary()` -> `EspeakProvider._find_binary()` (staticmethod)
   - `_load_voices_from_system()` -> `EspeakProvider._load_voices()` (classmethod)
   - `_rate_to_wpm(rate)` -> `EspeakProvider._rate_to_wpm()` (staticmethod)
3. **max_complexity**: The method with CC=14 needs `Extract Method` refactoring.

**init_violations fix**: `EspeakProvider.__init__` -> `EspeakProvider.__new__`

**Verification**: `make check` including `make check-oo`

---

#### Step 28: Fix `providers/__init__.py` -- method_ratio, class_to_func_ratio

**File**: `src/punt_vox/providers/__init__.py` (175 LOC, 2 failures)
**Failures**: `method_ratio=0.0`, `class_to_func_ratio=0.0`

**Class**: `ProviderRegistry`
**Module**: `src/punt_vox/providers/__init__.py`

**Constructor**:

```python
class ProviderRegistry:
    def __new__(cls) -> Self:
        self = super().__new__(cls)
        return self
```

**Methods**:

- `auto_detect_provider()` -> `ProviderRegistry.auto_detect()` (classmethod or staticmethod)
- `get_provider(name, **kwargs)` -> `ProviderRegistry.get(name, **kwargs)`
- `format_voice_hint(names, limit)` -> `ProviderRegistry.format_voice_hint(names, limit)` (staticmethod)
- `_register_polly(**kwargs)` -> `ProviderRegistry._register_polly(**kwargs)`
- `_register_openai(**kwargs)` -> `ProviderRegistry._register_openai(**kwargs)`
- `_register_elevenlabs(**kwargs)` -> `ProviderRegistry._register_elevenlabs(**kwargs)`
- `_register_say(**kwargs)` -> `ProviderRegistry._register_say(**kwargs)`
- `_register_espeak(**kwargs)` -> `ProviderRegistry._register_espeak(**kwargs)`
- `_has_aws_credentials()` -> `ProviderRegistry._has_aws_credentials()` (staticmethod)

**Module backward compat**: Re-export `auto_detect_provider = ProviderRegistry.auto_detect`
and `get_provider = ProviderRegistry.get` at module level. Remove once callers update.

**init_violations fix**: N/A (no `__init__` exists currently)

**Verification**: `make check` including `make check-oo`

---

### Tier 4: Small Utility Modules

Each of these files has 2 failures: `method_ratio=0.0` and
`class_to_func_ratio=0.0`. Each needs its module-level functions wrapped into
a class. The pattern is identical: create a class, move the functions to
methods, preserve the public API with module-level aliases during transition.

#### Step 29: Wrap `config.py` in a `ConfigStore` class

**File**: `src/punt_vox/config.py` (211 LOC, 2 failures)
**Failures**: `method_ratio=0.0`, `class_to_func_ratio=0.083`

**Class**: `ConfigStore`

`VoxConfig` already exists as a frozen dataclass (stays as-is -- it is a value
type, not a service). The 11 module-level functions all operate on config files.
They share `config_dir` as a parameter.

**Constructor**:

```python
class ConfigStore:
    def __new__(cls, config_dir: Path | None = None) -> Self:
        self = super().__new__(cls)
        self._config_dir = config_dir or find_config_dir() or DEFAULT_CONFIG_DIR
        return self
```

**Methods**:

- `read_field(field, config_dir)` -> `ConfigStore.read_field(field)`
- `read_config(config_dir)` -> `ConfigStore.read_config()`
- `write_field(key, value, config_dir)` -> `ConfigStore.write_field(key, value)`
- `write_fields(updates, config_dir)` -> `ConfigStore.write_fields(updates)`
- `_parse_frontmatter(path)` -> `ConfigStore._parse_frontmatter(path)` (staticmethod)
- `_fields_to_config(...)` -> `ConfigStore._fields_to_config(...)` (staticmethod)
- `_read_single_field(path, field)` -> `ConfigStore._read_single_field(path, field)` (staticmethod)
- `_write_single(path, key, value)` -> `ConfigStore._write_single(path, key, value)` (staticmethod)
- `_write_batch(path, updates)` -> `ConfigStore._write_batch(path, updates)` (staticmethod)
- `_validate_value(value)` -> `ConfigStore._validate_value(value)` (staticmethod)
- `_derive_repo_name(config_dir)` -> `ConfigStore._derive_repo_name(config_dir)` (staticmethod)

**init_violations fix**: N/A

**Verification**: `make check` including `make check-oo`

---

#### Step 30: Wrap `cache.py` functions in a `CacheManager` class

**File**: `src/punt_vox/cache.py` (142 LOC, 2 failures)
**Failures**: `method_ratio=0.0`, `class_to_func_ratio=0.143`

**Class**: `CacheManager`

`CacheInfo` already exists as a frozen dataclass (stays as-is).

**Constructor**:

```python
class CacheManager:
    def __new__(cls, cache_dir: Path | None = None) -> Self:
        self = super().__new__(cls)
        self._cache_dir = cache_dir or _default_cache_dir()
        return self
```

**Methods**:

- `cache_key(text, voice, provider)` -> `CacheManager.cache_key(text, voice, provider)` (staticmethod)
- `cache_get(text, voice, provider)` -> `CacheManager.get(text, voice, provider)`
- `cache_put(text, voice, provider, audio)` -> `CacheManager.put(text, voice, provider, audio)`
- `_evict_if_needed()` -> `CacheManager._evict_if_needed()`
- `cache_clear()` -> `CacheManager.clear()`
- `cache_status()` -> `CacheManager.status()`

**init_violations fix**: N/A

**Verification**: `make check` including `make check-oo`

---

#### Step 31: Wrap `dirs.py` in a `DirectoryResolver` class

**File**: `src/punt_vox/dirs.py` (105 LOC, 2 failures)
**Failures**: `method_ratio=0.0`, `class_to_func_ratio=0.0`

**Class**: `DirectoryResolver`

**Constructor**:

```python
class DirectoryResolver:
    def __new__(cls) -> Self:
        self = super().__new__(cls)
        return self
```

**Methods** (all become classmethods or staticmethods -- no instance state):

- `find_config_dir(start)` -> `DirectoryResolver.find_config_dir(start)` (staticmethod)
- `ephemeral_dir(repo_root)` -> `DirectoryResolver.ephemeral_dir(repo_root)` (staticmethod)
- `default_output_dir()` -> `DirectoryResolver.default_output_dir()` (staticmethod)
- `music_output_dir()` -> `DirectoryResolver.music_output_dir()` (staticmethod)
- `_parse_xdg_user_dir(key)` -> `DirectoryResolver._parse_xdg_user_dir(key)` (staticmethod)
- `_resolve_music_dir()` -> `DirectoryResolver._resolve_music_dir()` (staticmethod)

**init_violations fix**: N/A

**Verification**: `make check` including `make check-oo`

---

#### Step 32: Wrap `keys.py` functions in a `KeysManager` class

**File**: `src/punt_vox/keys.py` (101 LOC, 2 failures)
**Failures**: `method_ratio=0.0`, `class_to_func_ratio=0.0`

**Class**: `KeysManager`

The `PROVIDER_KEY_NAMES` constant stays module-level (it is a public constant
imported by other modules per Step 0).

**Constructor**:

```python
class KeysManager:
    def __new__(cls, keys_path: Path | None = None) -> Self:
        self = super().__new__(cls)
        self._keys_path = keys_path or keys_file_path()
        return self
```

**Methods**:

- `keys_file_path()` -> `KeysManager.default_path()` (staticmethod)
- `parse_keys_env(text)` -> `KeysManager.parse(text)` (staticmethod)
- `format_keys_env(keys)` -> `KeysManager.format(keys)` (staticmethod)
- `write_keys_env(env)` -> `KeysManager.write(env)`
- `load_keys_env()` -> `KeysManager.load()`

**init_violations fix**: N/A

**Verification**: `make check` including `make check-oo`

---

#### Step 33: Wrap `logging_config.py` in a `LoggingSetup` class

**File**: `src/punt_vox/logging_config.py` (46 LOC, 2 failures)
**Failures**: `method_ratio=0.0`, `class_to_func_ratio=0.0`

**Class**: `LoggingSetup`

**Constructor**:

```python
class LoggingSetup:
    def __new__(cls) -> Self:
        self = super().__new__(cls)
        return self
```

**Methods**:

- `configure_logging(*, stderr_level)` -> `LoggingSetup.configure(*, stderr_level)` (staticmethod)
- `_log_level_key(name)` -> `LoggingSetup._log_level_key(name)` (staticmethod)

**init_violations fix**: N/A

**Verification**: `make check` including `make check-oo`

---

#### Step 34: Wrap `mood.py` in a `MoodClassifier` class

**File**: `src/punt_vox/mood.py` (40 LOC, 2 failures)
**Failures**: `method_ratio=0.0`, `class_to_func_ratio=0.0`

**Class**: `MoodClassifier`

```python
class MoodClassifier:
    def __new__(cls) -> Self:
        self = super().__new__(cls)
        return self

    @staticmethod
    def classify(vibe: str | None) -> str: ...
```

**Methods**:

- `classify_mood(vibe)` -> `MoodClassifier.classify(vibe)` (staticmethod)

Module-level constant `_MOOD_MAP` becomes `MoodClassifier._MOOD_MAP` class variable.

**init_violations fix**: N/A

**Verification**: `make check` including `make check-oo`

---

#### Step 35: Wrap `music.py` in a `MusicPromptBuilder` class

**File**: `src/punt_vox/music.py` (98 LOC, 2 failures)
**Failures**: `method_ratio=0.0`, `class_to_func_ratio=0.0`

**Class**: `MusicPromptBuilder`

```python
class MusicPromptBuilder:
    def __new__(cls) -> Self:
        self = super().__new__(cls)
        return self
```

**Methods**:

- `vibe_to_prompt(vibe, vibe_tags, style, signals)` -> `MusicPromptBuilder.build(vibe, vibe_tags, style, signals)`
- `_layer_style_mood_feel(...)` -> `MusicPromptBuilder._layer_style_mood_feel(...)` (staticmethod)
- `_layer_time_of_day(hour)` -> `MusicPromptBuilder._layer_time_of_day(hour)` (staticmethod)
- `_layer_work_intensity(signals)` -> `MusicPromptBuilder._layer_work_intensity(signals)` (staticmethod)

**init_violations fix**: N/A

**Verification**: `make check` including `make check-oo`

---

#### Step 36: Wrap `output.py` in an `OutputResolver` class

**File**: `src/punt_vox/output.py` (28 LOC, 2 failures)
**Failures**: `method_ratio=0.0`, `class_to_func_ratio=0.0`

**Class**: `OutputResolver`

```python
class OutputResolver:
    def __new__(cls) -> Self:
        self = super().__new__(cls)
        return self

    @staticmethod
    def resolve(request: SynthesisRequest) -> Path: ...
```

**Methods**:

- `resolve_output_path(request)` -> `OutputResolver.resolve(request)` (staticmethod)

**init_violations fix**: N/A

**Verification**: `make check` including `make check-oo`

---

#### Step 37: Wrap `paths.py` in a `VoxPaths` class

**File**: `src/punt_vox/paths.py` (93 LOC, 2 failures)
**Failures**: `method_ratio=0.0`, `class_to_func_ratio=0.0`

**Class**: `VoxPaths`

```python
class VoxPaths:
    def __new__(cls, state_root: Path | None = None) -> Self:
        self = super().__new__(cls)
        self._state_root = state_root or cls._default_state_dir()
        return self
```

**Methods**:

- `user_state_dir()` -> `VoxPaths._default_state_dir()` (staticmethod)
- `config_dir()` -> `VoxPaths.config_dir()` property
- `log_dir()` -> `VoxPaths.log_dir()` property
- `run_dir()` -> `VoxPaths.run_dir()` property
- `cache_dir()` -> `VoxPaths.cache_dir()` property
- `keys_env_file()` -> `VoxPaths.keys_env_file()` property
- `ensure_user_dirs(state_root)` -> `VoxPaths.ensure_dirs()`
- `installed_version()` -> `VoxPaths.installed_version()` (staticmethod)

**init_violations fix**: N/A

**Verification**: `make check` including `make check-oo`

---

#### Step 38: Wrap `playback.py` in an `AudioPlayer` class

**File**: `src/punt_vox/playback.py` (74 LOC, 2 failures)
**Failures**: `method_ratio=0.0`, `class_to_func_ratio=0.0`

**Class**: `AudioPlayer`

```python
class AudioPlayer:
    def __new__(cls) -> Self:
        self = super().__new__(cls)
        return self
```

**Methods**:

- `resolve_player()` -> `AudioPlayer.resolve_player()` (staticmethod)
- `play_audio(path)` -> `AudioPlayer.play(path)` (staticmethod)
- `enqueue(path)` -> `AudioPlayer.enqueue(path)` (staticmethod)

**init_violations fix**: N/A

**Verification**: `make check` including `make check-oo`

---

#### Step 39: Wrap `quips.py` in a `QuipRegistry` class

**File**: `src/punt_vox/quips.py` (130 LOC, 2 failures)
**Failures**: `method_ratio=0.0`, `class_to_func_ratio=0.0`

**Note**: This file has only module-level tuple constants and zero functions.
The tool reports `method_ratio=0.0` and `class_to_func_ratio=0.0` because
it has more than 5 top-level statements (the tuple assignments) and no
classes or methods.

**Class**: `QuipRegistry`

```python
class QuipRegistry:
    STOP: ClassVar[tuple[str, ...]] = (...)
    PERMISSION: ClassVar[tuple[str, ...]] = (...)
    IDLE: ClassVar[tuple[str, ...]] = (...)
    PRE_COMPACT: ClassVar[tuple[str, ...]] = (...)
    ACKNOWLEDGE: ClassVar[tuple[str, ...]] = (...)
    SUBAGENT_START: ClassVar[tuple[str, ...]] = (...)
    SUBAGENT_STOP: ClassVar[tuple[str, ...]] = (...)
    FAREWELL: ClassVar[tuple[str, ...]] = (...)

    @staticmethod
    def pick(pool: tuple[str, ...]) -> str:
        import random
        return random.choice(pool)
```

All module-level constants (`STOP_PHRASES`, `PERMISSION_PHRASES`, etc.) become
class-level constants on `QuipRegistry`. Module-level aliases preserve backward
compatibility during transition.

**init_violations fix**: N/A

**Verification**: `make check` including `make check-oo`

---

#### Step 40: Wrap `resolve.py` in a `VoiceResolver` class

**File**: `src/punt_vox/resolve.py` (117 LOC, 3 failures)
**Failures**: `method_ratio=0.0`, `class_to_func_ratio=0.0`, `max_complexity=11`

**Class**: `VoiceResolver`

```python
class VoiceResolver:
    def __new__(cls) -> Self:
        self = super().__new__(cls)
        return self
```

**Methods**:

- `split_leading_expressive_tags(text)` -> `VoiceResolver.split_leading_expressive_tags(text)` (staticmethod)
- `strip_expressive_tags(text)` -> `VoiceResolver.strip_expressive_tags(text)` (staticmethod)
- `resolve_voice_and_language(...)` -> `VoiceResolver.resolve(...)` (staticmethod)
- `apply_vibe(...)` -> `VoiceResolver.apply_vibe(...)` (staticmethod)

**max_complexity fix**: Extract Method on the function with CC=11.

**init_violations fix**: N/A

**Verification**: `make check` including `make check-oo`

---

#### Step 41: Wrap `voices.py` in a `VoiceMetadata` class

**File**: `src/punt_vox/voices.py` (65 LOC, 2 failures)
**Failures**: `method_ratio=0.0`, `class_to_func_ratio=0.0`

**Class**: `VoiceMetadata`

```python
class VoiceMetadata:
    BLURBS: ClassVar[dict[str, str]] = { ... }

    @staticmethod
    def not_found_message(exc: VoiceNotFoundError) -> str: ...
```

**Methods**:

- `voice_not_found_message(exc)` -> `VoiceMetadata.not_found_message(exc)` (staticmethod)

`VOICE_BLURBS` dict becomes `VoiceMetadata.BLURBS` class variable.

**init_violations fix**: N/A

**Verification**: `make check` including `make check-oo`

---

#### Step 42: Wrap `applet.py` in a `VoxApplet` class

**File**: `src/punt_vox/applet.py` (150 LOC, 3 failures)
**Failures**: `method_ratio=0.0`, `class_to_func_ratio=0.0`, `avg_complexity=5.25`

**Class**: `VoxApplet`

```python
class VoxApplet:
    def __new__(cls) -> Self:
        self = super().__new__(cls)
        return self
```

**Methods**:

- `build_vox_elements(cfg, provider_name, vox_status, daemon_status)` -> `VoxApplet.build_elements(...)`
- `show_applet(cfg)` -> `VoxApplet.show(cfg)`
- `_is_hook_active(cfg, rule)` -> `VoxApplet._is_hook_active(cfg, rule)` (staticmethod)
- `_build_info_tooltip(cfg, provider_name)` -> `VoxApplet._build_info_tooltip(cfg, provider_name)` (staticmethod)

**avg_complexity fix**: `avg_complexity=5.25` exceeds the 5.0 threshold.
Wrapping functions into methods does not change their cyclomatic complexity.
Apply Extract Method to the most complex functions (likely `build_elements`
and `show`). Extract conditional sections (hook-status checks, tooltip
assembly, status-line construction) into dedicated private methods. Target:
`avg_complexity <= 5.0` after extraction.

**init_violations fix**: N/A

**Verification**: `make check` including `make check-oo`

---

#### Step 43: Fix `core.py` -- init_violations, method_ratio, class_to_func_ratio

**File**: `src/punt_vox/core.py` (300 LOC, 3 failures)
**Failures**: `method_ratio=0.692`, `class_to_func_ratio=0.2`, `init_violations=1`

**Changes**:

1. **init_violations**: `TTSClient.__init__` -> `TTSClient.__new__`

2. **Module functions become methods on `TTSClient`**:
   - `split_text(text, max_chars)` -> `TTSClient.split_text(text, max_chars)` (staticmethod)
   - `_split_at_words(text, max_chars)` -> `TTSClient._split_at_words(text, max_chars)` (staticmethod)
   - `_pad_audio_file(path)` -> `TTSClient._pad_audio_file(path)` (staticmethod)
   - `stitch_audio(segments, output_path, pause_ms)` -> `TTSClient.stitch_audio(segments, output_path, pause_ms)` (staticmethod)

**init_violations fix**: `TTSClient.__init__` -> `TTSClient.__new__`

**Verification**: `make check` including `make check-oo`

---

### Tier 5: Trivial

#### Step 44: Add `from __future__ import annotations` to `assets/__init__.py`

**File**: `src/punt_vox/assets/__init__.py` (2 LOC, 1 failure)
**Failures**: `future_annotations=0`

**Change**: Add `from __future__ import annotations` as the first line.

**Verification**: `make check` including `make check-oo`

---

### Tier 6: Remaining Gaps

These steps close the gaps identified in the peer review that were not
covered by Steps 0-44. They are independent of each other and can execute
in any order after the relevant Part 2 steps complete.

#### Step 45: Fix `providers/elevenlabs_music.py` -- init_violations

**File**: `src/punt_vox/providers/elevenlabs_music.py` (1 failure)
**Failures**: `init_violations=1`

**Changes**:

- **init_violations**: `ElevenLabsMusicProvider.__init__` -> `ElevenLabsMusicProvider.__new__`

Same pattern as Steps 23-27. Convert the constructor:

```python
class ElevenLabsMusicProvider:
    def __new__(cls, api_key: str | None = None) -> Self:
        self = super().__new__(cls)
        self._api_key = api_key
        return self
```

- **Module-level functions**: If any module-level helper functions exist,
  move them into `ElevenLabsMusicProvider` as methods or staticmethods
  (same pattern as the other provider steps).

**init_violations fix**: `ElevenLabsMusicProvider.__init__` -> `ElevenLabsMusicProvider.__new__`

**Verification**: `make check` including `make check-oo`

---

#### Step 46: Split `server.py` into class-based modules

**File**: `src/punt_vox/server.py` (~900 LOC, 5 failures)
**Failures**: `method_ratio=0.04`, `class_to_func_ratio=0.0`, `module_size=900`, `max_complexity=36`, `avg_complexity=6.36`

Section 3.0 describes this split but it had no numbered execution step.
This is that step.

**Classes**:

1. `SpeechTools` -- MCP tool methods for `unmute`, `speak`, `notify`, `record`,
   `who`, `status`
2. `MusicTools` -- MCP tool methods for music-related MCP tools
3. `ConfigTools` -- MCP tool methods for `vibe`, `show_vox`

**Module split**:

- `server.py` (~250 LOC): `SessionConfig` (from Step 16), FastMCP app
  creation, tool registration, `run_server()`. Imports tool classes and
  registers their methods as MCP tools.
- `server_speech.py` (~200 LOC): `SpeechTools` class with methods for
  speech-related MCP tools.
- `server_music.py` (~150 LOC): `MusicTools` class with methods for
  music-related MCP tools.
- `server_config.py` (~150 LOC): `ConfigTools` class with methods for
  config/display MCP tools.

Each tool class receives `SessionConfig` via constructor injection:

```python
class SpeechTools:
    def __new__(cls, session: SessionConfig) -> Self:
        self = super().__new__(cls)
        self._session = session
        return self

    def unmute(self, voice: str | None = None, ...) -> str: ...
    def speak(self, text: str, ...) -> str: ...
    def notify(self, text: str, ...) -> str: ...
    # ...
```

**max_complexity fix**: The function with CC=36 must have Extract Method
applied when it becomes a method on the appropriate tool class. Identify
the branches (provider resolution, voice resolution, config refresh,
error handling) and extract each into a private method. Target: no method
exceeds CC=10.

**avg_complexity fix**: Distributing tools across 3 classes in 3 files,
combined with Extract Method on the CC=36 function, brings `avg_complexity`
under 5.0 for each file.

**module_size**: Each resulting file is under 300 LOC.

**method_ratio / class_to_func_ratio**: Each file has a class with methods.
The MCP tool registration functions in `server.py` are thin wrappers that
delegate to tool class methods, keeping `class_to_func_ratio >= 0.5`.

**Verification**: `make check` including `make check-oo`

---

#### Step 47: Decompose `__main__.py` -- extract `cli_types.py`

**File**: `src/punt_vox/__main__.py` (~1700 LOC after DoctorCheck extraction, 5 failures)
**Failures**: `method_ratio=0.0`, `class_to_func_ratio=0.0`, `module_size=1700`, `max_complexity=38`, `avg_complexity=6.02`

This is the first of 4 steps to decompose `__main__.py`.

**What moves**: The ~35 `Annotated[...]` type aliases at the top of the file.
These are Typer parameter type definitions used across all CLI commands.

**New module**: `src/punt_vox/cli_types.py` (~80 LOC)

**Class**: `CliParams`

```python
class CliParams:
    """Namespace for Typer Annotated parameter types."""
    Voice: ClassVar[type] = Annotated[str | None, typer.Option(...)]
    Provider: ClassVar[type] = Annotated[str | None, typer.Option(...)]
    # ... all 35 type aliases as ClassVar
```

All CLI command files import parameter types from `CliParams`.

**Verification**: `make check` including `make check-oo`

---

#### Step 48: Decompose `__main__.py` -- extract `cli_daemon.py`

**What moves**: Daemon lifecycle commands: `install`, `uninstall`, `restart`,
`status`, `daemon` (the `vox daemon start/stop` subgroup). ~300 LOC.

**New module**: `src/punt_vox/cli_daemon.py`

**Class**: `DaemonCommands`

```python
class DaemonCommands:
    def __new__(cls) -> Self:
        self = super().__new__(cls)
        return self

    def install(self, ...) -> None: ...
    def uninstall(self, ...) -> None: ...
    def restart(self, ...) -> None: ...
    def status(self, ...) -> None: ...
```

A Typer sub-app is created in `cli_daemon.py` and added to the main app
in `__main__.py` via `app.add_typer(daemon_app)`. The command functions
become thin wrappers that construct `DaemonCommands` and delegate.

**Verification**: `make check` including `make check-oo`

---

#### Step 49: Decompose `__main__.py` -- extract `cli_music.py`

**What moves**: Music commands: `on`, `off`, `next`, `play`, `list`. ~250 LOC.

**New module**: `src/punt_vox/cli_music.py`

**Class**: `MusicCommands`

```python
class MusicCommands:
    def __new__(cls) -> Self:
        self = super().__new__(cls)
        return self

    def on(self, ...) -> None: ...
    def off(self, ...) -> None: ...
    def next(self, ...) -> None: ...
    def play(self, ...) -> None: ...
    def list_tracks(self, ...) -> None: ...
```

A Typer sub-app is created in `cli_music.py` and added to the main app.

**Verification**: `make check` including `make check-oo`

---

#### Step 50: Decompose `__main__.py` -- extract `cli_cache.py`

**What moves**: Cache commands: `cache clear`, `cache status`, `cache warm`.
~150 LOC.

**New module**: `src/punt_vox/cli_cache.py`

**Class**: `CacheCommands`

```python
class CacheCommands:
    def __new__(cls) -> Self:
        self = super().__new__(cls)
        return self

    def clear(self, ...) -> None: ...
    def status(self, ...) -> None: ...
    def warm(self, ...) -> None: ...
```

**Verification**: `make check` including `make check-oo`

---

#### Step 51: Decompose `__main__.py` -- reduce complexity and finalize

After Steps 47-50, `__main__.py` retains the core commands: `unmute`,
`record`, `vibe`, `notify`, `speak`, `voice`, `doctor` (thin wrapper),
`version`, `hook` (subgroup). Target: ~300 LOC.

**Class**: `CoreCommands`

```python
class CoreCommands:
    def __new__(cls) -> Self:
        self = super().__new__(cls)
        return self

    def unmute(self, ...) -> None: ...
    def record(self, ...) -> None: ...
    def vibe(self, ...) -> None: ...
    def notify(self, ...) -> None: ...
    def speak(self, ...) -> None: ...
    def voice(self, ...) -> None: ...
```

The Typer command functions become thin wrappers that construct
`CoreCommands` and delegate.

**max_complexity fix**: The function with CC=38 must be identified and have
Extract Method applied. If it is the `doctor` command, it was already
extracted in Step 17. If it is another command (likely `unmute` or `speak`),
extract the provider-resolution, voice-resolution, and synthesis branches
into private methods on `CoreCommands`. Target: no method exceeds CC=10.

**avg_complexity fix**: Distributing commands across 4 modules (cli_daemon,
cli_music, cli_cache, `__main__`) combined with Extract Method on complex
functions brings `avg_complexity` under 5.0.

**module_size**: `__main__.py` is ~300 LOC. Each `cli_*.py` is under 300 LOC.

**method_ratio / class_to_func_ratio**: Each file has a class. The Typer
command functions are thin wrappers (1-3 lines each) that delegate to class
methods. With the class methods counted, `method_ratio >= 0.80` and
`class_to_func_ratio >= 0.5`.

**Verification**: `make check` including `make check-oo`

---

### Part 3 Verification

After all 52 steps (0-51) complete:

```bash
uv run python tools/oo_score.py src/punt_vox/
# Expected: 0 failures across all 11 metrics on all files

uv run python tools/oo_score.py src/punt_vox/ --threshold
# Expected: every file shows PASS on every metric

make check
# Expected: exit 0
```

---

### Part 3 Execution Summary

| Tier | Steps | Files | Risk |
|------|-------|-------|------|
| 2: Significant refactoring | 18-22 | hooks.py, normalize.py, watcher.py, client.py, types.py | Medium (module splits, caller updates) |
| 3: Provider classes | 23-28 | 6 provider files | Low (init -> new + move functions) |
| 4: Small utility modules | 29-43 | 15 files | Low (wrap in class, preserve API) |
| 5: Trivial | 44 | assets/`__init__.py` | None (one-line add) |
| 6: Remaining gaps | 45-51 | elevenlabs_music.py, server.py, `__main__.py` | Medium (server split, CLI decomposition) |
| **Total Part 3** | **34 steps** | **all files** | |
| **Grand Total (Parts 2+3)** | **52 steps** | **all 32+ files** | |

Steps within a tier are independent and can execute in any order. Tier 5 can
execute at any time. Tiers 3 and 4 can execute in parallel with each other
and with Part 2. Tier 2 steps are independent of each other but some depend
on Part 2 (Step 22 splits `types.py`, which Part 2 imports from). Tier 6
steps are independent of each other; Step 46 (server.py) depends on Step 16
(SessionConfig); Steps 47-51 (`__main__.py`) depend on Step 17 (DoctorCheck).

The final state: every file in `src/punt_vox/` passes all 11 OO metrics.
Zero failures. `make check` green. Zero residual metric failures.
