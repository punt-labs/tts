# Class Responsibility Analysis: punt-vox

Generated: 2026-05-13

This is document 1 of 3 in the voxd architecture redesign:

1. **This document** — proposes 16 classes to replace the 2729-line voxd.py monolith
2. [Peer Review](oo-design-review.md) — reviews this proposal, adds 6 revisions (bringing the total to 18 classes)
3. [Refactoring Plan](oo-refactoring-plan.md) — step-by-step execution plan incorporating the review's revisions

## Calibration

The merchants/game codebase demonstrates proper OO at scale:

| Metric | merchants/game | punt-vox (current) |
|--------|---------------|-------------------|
| Total LOC | 1993 | 14028 |
| Files | 13 | 33 |
| Max module LOC | 363 (captain.py) | 2729 (voxd.py) |
| Classes | ~20 | ~15 |
| LOC per class | ~100 | ~935 |
| Module-level functions | 0 | ~120 |

merchants/game has zero module-level business logic. Every function is a method
on the class that owns the data it operates on. `Game` does not reach into
`Captain`'s internals; `Deck` owns its card list and discard pile. The
`RoundController` owns round state and enforces turn-taking invariants through
its own methods.

punt-vox is the inverse. `voxd.py` is 2729 lines with one mutable god-object
(`DaemonContext` with 20 fields) and ~50 module-level functions that receive
`ctx` as a parameter and mutate it freely. There is no encapsulation boundary
anywhere in the daemon.

## Current State: Module-by-Module Inventory

| Module | LOC | Problem |
|--------|-----|---------|
| `voxd.py` | 2729 | God module. `DaemonContext` is a 20-field mutable bag. All business logic is in free functions that receive `ctx`. |
| `__main__.py` | 1992 | Acceptable for a CLI module. But `doctor` alone is 300 lines. |
| `service.py` | 1151 | Process management and systemd/launchd templating and keys.env writing all mixed. No classes. |
| `server.py` | 992 | `SessionState` is a mutable bag with no methods. Module-global `_state` singleton mutated by every tool function. |
| `client.py` | 715 | Well-structured. `VoxClient` owns its connection. |
| `normalize.py` | 703 | 500 lines of data and 200 lines of pure functions. Acceptable. |
| `hooks.py` | 643 | Stateless dispatch. Acceptable. |
| `watcher.py` | 483 | `SessionWatcher` is well-structured. |
| `core.py` | 367 | `TTSClient` is clean. |
| `types.py` | 361 | Clean protocol + frozen dataclass module. |
| `config.py` | 268 | `VoxConfig` is a frozen dataclass with zero methods. |
| Others | <200 each | Acceptable. |

## The Core Problem: voxd.py

`DaemonContext` is the textbook god-object. It holds:

1. **Auth state**: `auth_token`
2. **Server metadata**: `port` and `start_time` and `daemon_version` and `client_count`
3. **Playback queue and result**: `playback_queue` and `last_playback`
4. **Dedup engines**: `chime_dedup` and `once_dedup`
5. **Music scheduler state**: `music_mode` and `music_style` and `music_owner` and `music_vibe` and `music_track` and `music_track_name` and `music_proc` and `music_state` and `music_changed` and `music_replay`

These are five distinct responsibilities sharing one mutable object. Every
function in the file receives `ctx` and reaches into whichever fields it
needs. There are no encapsulation boundaries.

The ~50 module-level functions break down into these clusters:

- **Playback** (~150 lines): `_play_audio` / `_playback_consumer` / `_probe_duration` / `_record_playback_result` / `_player_command` / `_player_binary_path` / `_snapshot_env` / `_truncate_stderr` / `_monotonic`
- **Synthesis pipeline** (~200 lines): `_synthesize_to_file` / `_try_direct_play` / `_run_play_directly_sync` / `_apply_vibe_for_synthesis` / `_model_supports_expressive_tags` / `_build_audio_request`
- **Music scheduler** (~500 lines): `_music_loop` / `_playback_wait_loop` / `_generate_music_track` / `_kill_music_proc` / `_music_backoff_sleep` / `_auto_track_name` / `_slugify` / `_music_player_command` / `_music_output_dir`
- **WebSocket message handlers** (~400 lines): `_handle_synthesize` / `_handle_record` / `_handle_chime` / `_handle_voices` / `_handle_health` / `_handle_music_on` / `_handle_music_off` / `_handle_music_play` / `_handle_music_list` / `_handle_music_vibe` / `_handle_music_next`
- **Dedup** (~200 lines): `ChimeDedup` / `OnceDedup` / `DedupHit` (already classes)
- **Auth/config/logging/startup** (~300 lines): `_load_keys` / `_read_or_create_token` / `_configure_logging` / `_log_voxd_environment` / port file helpers
- **Health** (~50 lines): `_health_payload_minimal` / `_health_payload_full`
- **App factory + CLI** (~100 lines): `build_app` / `main` / `_ws_route` / `_health_route`

## Proposed Class Design

### 1. PlaybackQueue

- **Status**: PROPOSED
- **Module**: `src/punt_vox/voxd/playback.py`
- **Responsibility**: Owns the audio playback queue and the consumer loop and audio probing and the playback mutex.
- **Owns**: `_queue` (asyncio.Queue) / `_mutex` (asyncio.Lock) / `_last_result` (dict or None) / `_consumer_task` (asyncio.Task or None)
- **Uses**: Nothing. Pure audio infrastructure.
- **Public methods**: `enqueue(item)` / `start_consumer()` / `stop_consumer()` / `last_result` property / `queue_size` property
- **Estimated lines**: 200
- **Notes**: Absorbs `_play_audio` / `_playback_consumer` / `_probe_duration` / `_record_playback_result` / `_player_command` / `_player_binary_path` / `_snapshot_env` / `_truncate_stderr` / `_monotonic` / `PlaybackItem`. The mutex is internal -- callers never see it.

### 2. SynthesisPipeline

- **Status**: PROPOSED
- **Module**: `src/punt_vox/voxd/synthesis.py`
- **Responsibility**: Owns the synthesis process: vibe application and cache integration and provider construction and API key injection.
- **Owns**: `_env_lock` (asyncio.Lock)
- **Uses**: `punt_vox.cache` for cache_get/cache_put; `punt_vox.providers` for get_provider; `punt_vox.core.TTSClient`
- **Public methods**: `synthesize_to_file(...)` returning Path / `try_direct_play(...)` returning int or None or Exception
- **Estimated lines**: 350
- **Notes**: Absorbs `_synthesize_to_file` / `_try_direct_play` / `_run_play_directly_sync` / `_apply_vibe_for_synthesis` / `_model_supports_expressive_tags` / `_build_audio_request`. The env_lock serializes os.environ mutation -- it belongs to the class that does the mutation. `_apply_vibe_for_synthesis` and `_model_supports_expressive_tags` become `@staticmethod` since they are pure.

### 3. MusicScheduler

- **Status**: PROPOSED
- **Module**: `src/punt_vox/voxd/music_scheduler.py`
- **Responsibility**: Owns the music generation loop and track lifecycle and gapless handoff and playlist state.
- **Owns**: `_mode` / `_style` / `_owner` / `_vibe` / `_track` / `_track_name` / `_proc` / `_state` / `_changed` / `_replay` / `_loop_task`
- **Uses**: `PlaybackQueue` (for the playback mutex during music play -- or uses its own subprocess-based playback at reduced volume which does not go through the queue)
- **Public methods**: `start_loop()` / `stop_loop()` / `turn_on(owner_id ...)` / `turn_off()` / `play_track(name ...)` / `list_tracks()` / `update_vibe(owner_id ...)` / `skip_next(owner_id)` / `mode` property / `state` property / `track_name` property
- **Estimated lines**: 480
- **Notes**: Absorbs all `_music_*` functions and `_handle_music_*` handlers and `_PlaybackWaitResult` and `_auto_track_name` and `_slugify` and `_music_player_command` and `_music_output_dir`. This is the largest class because music scheduling is genuinely complex. Just under the 500-line limit.

### 4. ChimeDedup (EXISTS)

- **Status**: EXISTS
- **Module**: `src/punt_vox/voxd/dedup.py`
- **Responsibility**: Always-on in-memory dedup for chime signals.
- **Owns**: `_window` / `_seen`
- **Uses**: Nothing.
- **Public methods**: `should_play(signal) -> bool`
- **Estimated lines**: 30
- **Notes**: Already a proper class. Move to `dedup.py` alongside `OnceDedup`.

### 5. OnceDedup (EXISTS)

- **Status**: EXISTS
- **Module**: `src/punt_vox/voxd/dedup.py`
- **Responsibility**: Opt-in per-call speech dedup with caller-specified TTL.
- **Owns**: `_seen` dict of (monotonic / wall_clock) tuples
- **Uses**: Nothing.
- **Public methods**: `check_and_record(text / ttl_seconds) -> DedupHit or None` / `rollback(text)`
- **Estimated lines**: 100
- **Notes**: Already a proper class. Move to `dedup.py`. Keep `DedupHit` frozen dataclass in same file.

### 6. WebSocketRouter

- **Status**: PROPOSED
- **Module**: `src/punt_vox/voxd/router.py`
- **Responsibility**: Owns WebSocket message dispatch and auth checking and client lifecycle tracking.
- **Owns**: `_handlers` dict / `_auth_token` / `_client_count`
- **Uses**: `SynthesisPipeline` / `PlaybackQueue` / `MusicScheduler` / `ChimeDedup` / `OnceDedup`
- **Public methods**: `handle_connection(websocket)` / `client_count` property
- **Estimated lines**: 350
- **Notes**: Absorbs `_ws_route` / `_check_auth` / `_HANDLERS` dict / all `_handle_*` functions. Each handler becomes a private method. The router is the composition root that wires the domain objects together for each request. `_parse_optional_float` / `_parse_optional_int` / `_parse_optional_str` become `@staticmethod`.

### 7. DaemonHealth

- **Status**: PROPOSED
- **Module**: `src/punt_vox/voxd/health.py`
- **Responsibility**: Computes health payloads for authenticated and unauthenticated callers.
- **Owns**: `_start_time` / `_daemon_version` / `_port`
- **Uses**: `PlaybackQueue` (for queue size) / `WebSocketRouter` (for client count)
- **Public methods**: `minimal_payload() -> dict` / `full_payload() -> dict`
- **Estimated lines**: 60
- **Notes**: Absorbs `_health_payload_minimal` / `_health_payload_full`. Owns its own start_time and version -- these are read-only metadata not shared mutable state.

### 8. DaemonConfig

- **Status**: PROPOSED
- **Module**: `src/punt_vox/voxd/config.py`
- **Responsibility**: Owns startup configuration: key loading and auth token management and port file lifecycle and logging setup.
- **Owns**: `_run_dir` / `_config_dir` / `_log_dir` / `_auth_token` / `_port`
- **Uses**: Nothing. Pure startup infrastructure.
- **Public methods**: `load_keys()` / `read_or_create_token()` / `write_port_file(port)` / `remove_port_file()` / `configure_logging()` / `auth_token` property / `port` property
- **Estimated lines**: 200
- **Notes**: Absorbs `_load_keys` / `_read_or_create_token` / `_write_port_file` / `_remove_port_file` / `_configure_logging` / `_log_voxd_environment` / `_PROVIDER_KEY_NAMES` / `_TokenRedactFilter` / `_install_token_redact_filter`.

### 9. ChimeResolver

- **Status**: PROPOSED
- **Module**: `src/punt_vox/voxd/chimes.py`
- **Responsibility**: Resolves chime signal names to bundled asset paths.
- **Owns**: `_chime_map` dict (immutable after construction)
- **Uses**: Nothing.
- **Public methods**: `resolve(signal) -> Path or None`
- **Estimated lines**: 40
- **Notes**: Absorbs `_CHIME_MAP` / `_resolve_chime`. Small but has a clear single responsibility.

### 10. VoxDaemon (replaces DaemonContext)

- **Status**: PROPOSED (replaces `DaemonContext`)
- **Module**: `src/punt_vox/voxd/daemon.py`
- **Responsibility**: Composition root. Wires domain objects together and owns the Starlette app lifecycle and starts/stops background tasks.
- **Owns**: References to `DaemonConfig` / `PlaybackQueue` / `SynthesisPipeline` / `MusicScheduler` / `DaemonHealth` / `WebSocketRouter` / `ChimeDedup` / `OnceDedup` / `ChimeResolver`
- **Uses**: All of the above (via constructor injection)
- **Public methods**: `build_app() -> Starlette` / `run(host / port)`
- **Estimated lines**: 120
- **Notes**: This is the thin orchestrator. It creates the domain objects and wires them together and hands them to Starlette. It does NOT hold any domain state itself -- every piece of state lives in the class that owns it. The current `main()` function and `build_app()` become methods here.

### 11. ServiceInstaller (refactor of service.py)

- **Status**: PROPOSED
- **Module**: `src/punt_vox/service/installer.py`
- **Responsibility**: Cross-platform daemon service installation and uninstallation.
- **Owns**: `_platform` / `_user` / `_state_root`
- **Uses**: `KeysEnvWriter` / platform-specific backends
- **Public methods**: `install() -> str` / `uninstall() -> str` / `is_running() -> bool` / `detect_platform() -> str`
- **Estimated lines**: 200
- **Notes**: The current `service.py` (1151 lines) mixes: (a) keys.env writing / (b) process management (find/kill PIDs) / (c) launchd templating / (d) systemd templating / (e) legacy cleanup. Split into `ServiceInstaller` + platform backends + `ProcessManager` + `KeysEnvWriter`.

### 12. KeysEnvWriter

- **Status**: PROPOSED
- **Module**: `src/punt_vox/service/keys_env.py`
- **Responsibility**: Reads and writes keys.env with merge semantics and secure file creation.
- **Owns**: `_keys_path` / `_provider_key_names`
- **Uses**: Nothing.
- **Public methods**: `write(env) -> Path`
- **Estimated lines**: 100
- **Notes**: Absorbs `_write_keys_env` / `_PROVIDER_KEY_NAMES` from `service.py`. The security-critical `os.open` + `chmod` logic lives in one place.

### 13. ProcessManager

- **Status**: PROPOSED
- **Module**: `src/punt_vox/service/process.py`
- **Responsibility**: Find and kill daemon processes by port and read/write port files.
- **Owns**: `_default_port`
- **Uses**: Nothing.
- **Public methods**: `find_pid_on_port(port)` / `is_vox_daemon_process(pid)` / `kill_pid(pid)` / `kill_stale_daemon()` / `ensure_port_free()` / `read_port_file()`
- **Estimated lines**: 150
- **Notes**: Absorbs `_find_pid_on_port` / `_is_vox_daemon_process` / `_kill_pid` / `_kill_stale_daemon` / `_ensure_port_free` / `read_port_file` / `_remove_port_file` from `service.py`.

### 14. LaunchdBackend

- **Status**: PROPOSED
- **Module**: `src/punt_vox/service/launchd.py`
- **Responsibility**: macOS launchd plist generation and load/unload/kickstart.
- **Owns**: `_label` / `_plist_path`
- **Uses**: Nothing.
- **Public methods**: `plist_content(user)` / `stop()` / `install(user)` / `uninstall()` / `is_running()`
- **Estimated lines**: 150
- **Notes**: Absorbs `_launchd_*` functions from `service.py`.

### 15. SystemdBackend

- **Status**: PROPOSED
- **Module**: `src/punt_vox/service/systemd.py`
- **Responsibility**: Linux systemd unit generation and start/stop/enable/restart.
- **Owns**: `_unit_path`
- **Uses**: Nothing.
- **Public methods**: `unit_content(user)` / `stop()` / `install(user)` / `uninstall()` / `is_running()` / `cleanup_stale_user_unit()`
- **Estimated lines**: 200
- **Notes**: Absorbs `_systemd_*` functions and `_cleanup_stale_user_unit` from `service.py`.

### 16. SessionConfig (refactor of server.py SessionState)

- **Status**: NEEDS REFACTOR (rename + add methods)
- **Module**: `src/punt_vox/server.py`
- **Responsibility**: MCP session state: notification mode and voice and vibe and music mode. Owns its own refresh logic.
- **Owns**: `session_id` / `notify` / `speak` / `voice` / `provider` / `model` / `vibe_mode` / `vibe` / `vibe_tags` / `vibe_signals` / `music_mode`
- **Uses**: `punt_vox.config` (for reading/writing disk config)
- **Public methods**: `refresh_from_config()` / `update_provider(provider)` / `update_model(model)` / `update_vibe_tags(tags)` / `effective_voice(override)` / `effective_provider(override)` / `to_status_dict()`
- **Estimated lines**: 100
- **Notes**: The current `SessionState` is a dataclass with zero methods. The `_refresh_state_from_config` module-level function mutates it from outside. Move the refresh logic and field-update logic into methods on the class itself. The MCP tool functions then call `self._session.refresh_from_config()` instead of the module-level function.

### Modules that stay as-is (no class extraction needed)

| Module | LOC | Rationale |
|--------|-----|-----------|
| `types.py` | 361 | Protocol definitions + frozen dataclasses. Already proper OO. |
| `client.py` | 715 | `VoxClient` and `VoxClientSync` are well-structured classes. |
| `core.py` | 367 | `TTSClient` is a clean orchestrator. |
| `config.py` | 268 | `VoxConfig` is a frozen dataclass. Read/write functions are stateless. |
| `normalize.py` | 703 | Pure stateless text transformation. Data tables + pure functions. |
| `music.py` | 105 | `vibe_to_prompt` is a pure function. |
| `cache.py` | 174 | Module-level functions on a well-defined cache directory. |
| `hooks.py` | 643 | Stateless dispatch. Each reads config and calls voxd and returns. |
| `watcher.py` | 483 | `SessionWatcher` is already a proper class. |
| `playback.py` | 120 | Single `play_audio` function. Pure. |
| `applet.py` | 174 | Lux display builder. Stateless. |
| `dirs.py` | 106 | Path resolution. Pure functions. |
| `resolve.py` | 144 | Voice resolution helpers. Pure functions. |
| `output.py` | 26 | Thin wrapper. |
| `paths.py` | 114 | Path resolution. Pure functions. |
| `voices.py` | 72 | Data constants. |
| `quips.py` | 130 | Data constants. |
| `mood.py` | 56 | Pure function. |
| `keys.py` | 125 | Key loading. Stateless. |
| `logging_config.py` | 75 | Logging setup. Stateless. |
| `providers/*.py` | 126-440 each | Each provider is already a class implementing `TTSProvider`. |

### \_\_main\_\_.py (1992 lines)

The CLI module is inherently function-based (typer commands). The main
improvement is extracting the `doctor` command (~300 lines) into a
`DoctorCheck` class or separate module. The rest is plumbing that
connects CLI arguments to `VoxClientSync` calls -- that is what a CLI
module does. No class extraction proposed for the remaining commands.

## Extraction Order

The dependency graph determines the order. Extract leaves first then
composites.

```text
Phase 1 (no dependencies):
  1. ChimeDedup + OnceDedup + DedupHit  ->  voxd/dedup.py       (move)
  2. ChimeResolver                       ->  voxd/chimes.py      (extract)
  3. PlaybackQueue                       ->  voxd/playback.py    (extract)
  4. DaemonConfig                        ->  voxd/config.py      (extract)

Phase 2 (depends on Phase 1):
  5. SynthesisPipeline                   ->  voxd/synthesis.py   (extract)
  6. MusicScheduler                      ->  voxd/music_scheduler.py (extract)
  7. DaemonHealth                        ->  voxd/health.py      (extract)

Phase 3 (depends on Phase 2):
  8. WebSocketRouter                     ->  voxd/router.py      (extract)

Phase 4 (composition root):
  9. VoxDaemon                           ->  voxd/daemon.py      (extract)
 10. voxd.py becomes voxd/__init__.py    ->  re-exports entrypoint()

Phase 5 (service.py decomposition -- independent of voxd):
 11. KeysEnvWriter                       ->  service/keys_env.py
 12. ProcessManager                      ->  service/process.py
 13. LaunchdBackend                      ->  service/launchd.py
 14. SystemdBackend                      ->  service/systemd.py
 15. ServiceInstaller                    ->  service/installer.py

Phase 6 (server.py):
 16. SessionConfig refactor              ->  server.py (in-place)
```

## Dependency Graph

```text
VoxDaemon (composition root)
  |-- DaemonConfig          (startup: keys / token / port file / logging)
  |-- PlaybackQueue         (audio queue + consumer loop)
  |-- ChimeDedup            (chime signal dedup)
  |-- OnceDedup             (speech per-call dedup)
  |-- ChimeResolver         (signal -> asset path)
  |-- SynthesisPipeline     (TTS synthesis + cache + env lock)
  |-- MusicScheduler        (music loop + track lifecycle)
  |-- DaemonHealth          (health payloads)
  |-- WebSocketRouter       (message dispatch + auth)
       |-- SynthesisPipeline
       |-- PlaybackQueue
       |-- MusicScheduler
       |-- ChimeDedup
       |-- OnceDedup
       |-- ChimeResolver
       |-- DaemonHealth

ServiceInstaller
  |-- KeysEnvWriter
  |-- ProcessManager
  |-- LaunchdBackend
  |-- SystemdBackend
```

No cycles. Every dependency points downward. Domain objects do not
depend on the router or the daemon. The router depends on domain objects.
The daemon depends on everything but only as a wiring layer.

## Summary Table

| # | Class | Status | Module | Est. LOC |
|---|-------|--------|--------|----------|
| 1 | `PlaybackQueue` | PROPOSED | `voxd/playback.py` | 200 |
| 2 | `SynthesisPipeline` | PROPOSED | `voxd/synthesis.py` | 350 |
| 3 | `MusicScheduler` | PROPOSED | `voxd/music_scheduler.py` | 480 |
| 4 | `ChimeDedup` | EXISTS | `voxd/dedup.py` | 30 |
| 5 | `OnceDedup` | EXISTS | `voxd/dedup.py` | 100 |
| 6 | `WebSocketRouter` | PROPOSED | `voxd/router.py` | 350 |
| 7 | `DaemonHealth` | PROPOSED | `voxd/health.py` | 60 |
| 8 | `DaemonConfig` | PROPOSED | `voxd/config.py` | 200 |
| 9 | `ChimeResolver` | PROPOSED | `voxd/chimes.py` | 40 |
| 10 | `VoxDaemon` | PROPOSED | `voxd/daemon.py` | 120 |
| 11 | `ServiceInstaller` | PROPOSED | `service/installer.py` | 200 |
| 12 | `KeysEnvWriter` | PROPOSED | `service/keys_env.py` | 100 |
| 13 | `ProcessManager` | PROPOSED | `service/process.py` | 150 |
| 14 | `LaunchdBackend` | PROPOSED | `service/launchd.py` | 150 |
| 15 | `SystemdBackend` | PROPOSED | `service/systemd.py` | 200 |
| 16 | `SessionConfig` | NEEDS REFACTOR | `server.py` | 100 |

**Totals**: 16 classes across 15 modules. No module exceeds 500 lines.
No class exceeds 20 methods. No god objects. Every piece of business
logic is a method on the class that owns the data it operates on.

The largest proposed class (`MusicScheduler` at 480 lines) is at the
limit. If it creeps during implementation split generation into a
`TrackGenerator` class that `MusicScheduler` delegates to.
