# Music Package Design — `voxd/music/`

Date: 2026-05-14
Author: claude (COO)
Reviewer: Ralph Johnson (rej)
Status: GO — revised per review feedback

## Problem

The music subsystem spans 4 files in `voxd/`:

| File | Lines | Failures | Root cause |
|------|-------|----------|------------|
| `music_handlers.py` | 331 | `classes_per_module=6`, `module_size=331` | 6 handler classes in one file |
| `music_scheduler.py` | 464 | `max_complexity=20`, `module_size=464` | `loop()` is a monolithic state machine; handlers mutate scheduler state from outside |
| `track_generator.py` | 95 | 0 | Clean but undersized — track lookup logic lives in handlers instead |

The design problem behind the metric failures: handlers contain domain logic.
`MusicOnHandler.__call__` makes 8 imperative state mutations on `MusicScheduler`
(`mode`, `style`, `owner`, `vibe`, `track`, `track_name`, `state`, `replay`,
`changed.set()`). The handler decides whether to kill the proc, whether to
replay, which fields to set. The scheduler's `loop()` then wakes up and reads
state that someone else wrote. Two objects co-manage one piece of state.

Current sequence for `music_on` (new track):

```
MusicOnHandler                    MusicScheduler
     │                                  │
     │──.mode = "on"──────────────────>│
     │──.style = "techno"─────────────>│
     │──.owner = "abc"────────────────>│
     │──.vibe = (...)─────────────────>│
     │──.track_name = ""──────────────>│
     │──.replay = False───────────────>│
     │──.state = "generating"─────────>│
     │──.changed.set()────────────────>│
     │                           (loop wakes, reads state)
```

This is the same anti-pattern the original `DaemonContext` had: a mutable bag
with external code reaching in to set fields.

## Proposed Design

### Package: `src/punt_vox/voxd/music/`

Music is a self-contained subsystem with its own lifecycle (generation, playback
loop, track storage). It shares nothing with speech synthesis except the
WebSocket transport. Extracting it into a subpackage makes the boundary explicit.

### Corrected sequence

```
MusicOnHandler              MusicScheduler              TrackGenerator
     │                            │                           │
     │──turn_on(owner,───────────>│                           │
     │   style, vibe, name)       │──find_track(name)───────>│
     │                            │<──path | None─────────────│
     │                            │                           │
     │                            │──(kill_proc if needed)    │
     │                            │──(set internal state)     │
     │                            │──(changed.set)            │
     │                            │                           │
     │<──MusicResponse────────────│                           │
     │   (status, track, name)    │                           │
```

One call across the boundary. The scheduler owns the decision. The handler
parses the message and sends the response.

### Module layout

| File | Class | Lines (est.) | Responsibility |
|------|-------|-------------|----------------|
| `__init__.py` | — | ~40 | Re-exports |
| `types.py` | `MusicResponse`, `MusicMode`, `MusicStatus`, `MusicState` | ~35 | Frozen dataclass returned by scheduler domain methods; `Literal` types for mode/status/state |
| `scheduler.py` | `MusicScheduler` | ~280 | Domain methods (`turn_on`, `turn_off`, `play_track`, `update_vibe`, `skip_next`), `loop()` orchestration, `_kill_proc()`. All state private. Read-only properties for inspection. `_PlaybackWaitResult` stays here (private, not exported). |
| `generator.py` | `TrackGenerator` | ~120 | Moved from `voxd/track_generator.py`. Absorbs `find_track(name) -> Path | None` (track lookup currently inline in handlers). |
| `on_handler.py` | `MusicOnHandler(MessageHandler)` | ~30 | Parse → `scheduler.turn_on(...)` → serialize `MusicResponse` → send |
| `off_handler.py` | `MusicOffHandler(MessageHandler)` | ~15 | Parse → `scheduler.turn_off()` → send |
| `play_handler.py` | `MusicPlayHandler(MessageHandler)` | ~25 | Parse → `scheduler.play_track(...)` → send |
| `list_handler.py` | `MusicListHandler(MessageHandler)` | ~15 | Parse → `generator.list_tracks()` → send |
| `vibe_handler.py` | `MusicVibeHandler(MessageHandler)` | ~20 | Parse → `scheduler.update_vibe(...)` → send |
| `next_handler.py` | `MusicNextHandler(MessageHandler)` | ~15 | Parse → `scheduler.skip_next(...)` → send |

Every module: ≤ 300 lines, ≤ 3 classes, `from __future__ import annotations`,
`__all__`, `__slots__`, `__new__` (not `__init__`).

`MessageHandler` protocol stays in `voxd/types.py` — it is shared across
speech, music, and system handlers.

### Types (`music/types.py`)

```python
from typing import Literal

MusicMode = Literal["off", "on"]
MusicState = Literal["idle", "generating", "playing"]
MusicStatus = Literal["generating", "playing", "stopped", "ignored"]

@dataclass(frozen=True, slots=True)
class MusicResponse:
    status: MusicStatus
    track: str | None = None   # str(path) when relevant
    name: str | None = None    # slugified track name when relevant
```

`MusicMode` and `MusicState` are used internally by the scheduler for type-safe
state fields. `MusicStatus` types the response. `Literal` types catch typos at
type-check time (PY-TS-8).

`MusicResponse` is a success-path type. Validation failures (`ValueError`) are
raised by scheduler domain methods and caught by handlers, which translate them
to error JSON. `"ignored"` is not an error — it is a normal non-exceptional
outcome (PY-EH-4).

### `MusicScheduler` domain methods

Current writable property setters (11 pairs on lines 92–189) are removed.
State is private with read-only properties where external inspection is needed
(e.g., `mode` for daemon health, `state` for status reporting).

```
async turn_on(owner_id: str, style: str, vibe: tuple[str, str], name: str) -> MusicResponse
    - Validates owner_id non-empty (ValueError if not)
    - If name non-empty, validates slugified name non-empty (ValueError if not)
    - Delegates track lookup to TrackGenerator.find_track(name)
    - If existing track found: _kill_proc, set replay state, return status="playing"
    - If new owner or not playing: _kill_proc
    - Set internal state (mode, style, owner, vibe, track_name, replay, state)
    - Signal changed event
    - Return status="generating"

async turn_off() -> MusicResponse
    - _kill_proc
    - Set mode="off", state="idle", replay=False
    - Signal changed
    - Return status="stopped"

async play_track(name: str, owner_id: str) -> MusicResponse
    - Validates name and owner_id non-empty (ValueError if not)
    - Validates slugified name non-empty (ValueError if not)
    - Delegates track lookup to TrackGenerator.find_track(name)
    - If not found: raise ValueError
    - _kill_proc, set replay state
    - Return status="playing"

update_vibe(owner_id: str, vibe: tuple[str, str]) -> MusicResponse
    - Validates owner_id non-empty (ValueError if not)
    - If owner_id != self._owner: return status="ignored"
    - If vibe unchanged: return status="ignored"
    - Set vibe, signal changed
    - Return status="generating"

skip_next(owner_id: str) -> MusicResponse
    - Validates owner_id non-empty (ValueError if not)
    - If mode != "on": return status="ignored"
    - Clear track_name, clear replay, signal changed
    - Return status="generating"
```

Parameter counts: `turn_on` has 4 (PY-OO-3 compliant — `vibe` is
`tuple[str, str]`, matching the internal representation). All others have ≤ 2.

Validation: all domain methods validate their own preconditions and raise
`ValueError` for invalid inputs (PY-EH-1). Handlers catch `ValueError` and
translate to error JSON. No validation logic in handlers.

`_kill_proc` is private — no handler calls it directly after the refactor.

### `TrackGenerator` additions

```
find_track(name) -> Path | None
    - Slugify name
    - If empty after slugify: return None (caller decides if error)
    - Check output_dir / f"{safe_name}.mp3" exists
    - Return path or None
```

This absorbs the track-lookup logic currently duplicated in `MusicOnHandler`
(lines 63–70 of current `music_handlers.py`) and `MusicPlayHandler`
(lines 171–187).

### `loop()` complexity reduction

Current `loop()` is CC=20. After domain methods absorb state-transition logic,
`loop()` only handles async coordination: wait for mode, generate or replay,
spawn subprocess, race events.

Extract Method targets:

1. `_run_initial_generation() -> Path` — lines 240–264: the replay-or-generate
   block that runs when no current track exists.
2. `_handle_generation_complete(gen_task, retry_count) -> tuple[Path | None, int]` —
   lines 421–472: extract the success/failure branching when a generation task
   finishes.
3. `_handle_vibe_change(gen_task) -> tuple[asyncio.Task | None, Path | None]` —
   lines 474–505: cancel old gen, start new or handle replay.

Each extracted method is ≤ CC=5. `loop()` becomes a dispatcher that calls them.
`_playback_wait_loop()` shrinks correspondingly.

Target: `loop()` CC ≤ 8, `_playback_wait_loop()` CC ≤ 8, `scheduler.py` ≤ 280
lines.

### Wiring changes in `daemon.py`

`_build_handler_dict` imports from `punt_vox.voxd.music` instead of
`punt_vox.voxd.music_handlers` and `punt_vox.voxd.music_scheduler`. The handler
construction changes from:

```python
"music_on": MusicOnHandler(music=music, track_generator=track_generator),
```

to:

```python
"music_on": MusicOnHandler(scheduler=music),
```

Handlers that currently take `track_generator` no longer need it — the
scheduler owns the TrackGenerator reference and delegates internally.
Exception: `MusicListHandler` still takes `TrackGenerator` directly (it doesn't
go through the scheduler — listing tracks is a read-only query on the
generator, not a state transition).

### Test strategy

Tests mirror source: one test file per source module.

| Test file | Tests | Constructs | Mocks |
|-----------|-------|------------|-------|
| `tests/music/conftest.py` | Shared fixtures: mock scheduler factory, mock websocket, mock `send_json`. | — | — |
| `tests/music/test_scheduler.py` | Domain methods: `turn_on` sets correct state, `turn_off` kills proc, `play_track` finds/replays, `update_vibe` rejects non-owner, `skip_next` when off. Validation: `turn_on` rejects empty owner_id (`ValueError`), `play_track` rejects empty name (`ValueError`), `play_track` raises on track not found. No WebSocket. | Real `MusicScheduler` | Mock `TrackGenerator` (for `find_track`, `generate`) |
| `tests/music/test_scheduler_loop.py` | `loop()` + `_playback_wait_loop()` async coordination: initial generation, vibe change during playback, replay, music-off, retry/backoff. **Must include**: max retries disables music (`_MUSIC_MAX_RETRIES` consecutive failures → mode="off"). | Real `MusicScheduler` | Mock subprocess, mock `TrackGenerator.generate` |
| `tests/music/test_generator.py` | `find_track`, `list_tracks`, `slugify`, `auto_track_name`. Pure filesystem tests. | Real `TrackGenerator` | `tmp_path` fixture |
| `tests/music/test_on_handler.py` | Parse wire message → `scheduler.turn_on()` called with correct args → response serialized from `MusicResponse`. **Also**: handler catches `ValueError` from scheduler → sends error JSON. | `MusicOnHandler` | Mock `MusicScheduler` (mock `turn_on` returns `MusicResponse` or raises `ValueError`) |
| `tests/music/test_off_handler.py` | Same pattern. | `MusicOffHandler` | Mock `turn_off` |
| `tests/music/test_play_handler.py` | Same pattern + handler catches `ValueError` for missing name/owner_id/track-not-found. | `MusicPlayHandler` | Mock `play_track` |
| `tests/music/test_list_handler.py` | Same pattern. | `MusicListHandler` | Mock `TrackGenerator` |
| `tests/music/test_vibe_handler.py` | Same pattern + handler catches `ValueError` for empty owner_id. | `MusicVibeHandler` | Mock `update_vibe` |
| `tests/music/test_next_handler.py` | Same pattern + handler catches `ValueError` for empty owner_id. | `MusicNextHandler` | Mock `skip_next` |

**Handler tests verify**: correct fields parsed from message, domain method
called with right args, `MusicResponse` serialized to WebSocket, `ValueError`
caught and translated to error JSON. They do not inspect scheduler internal
state. A mock `send_json` is the only WebSocket fake.

**Scheduler tests verify**: state transitions, ownership rules, kill-on-transfer,
replay flag, changed-event signaling, validation rejections. They do not parse
wire messages.

**The boundary is enforced by mocking**: handler tests mock the scheduler (can't
accidentally test domain logic). Scheduler tests have no WebSocket (can't
accidentally test protocol parsing).

Shared fixtures in `tests/music/conftest.py` per PL-TT-4 (test infrastructure
is first-class code).

### Deleted files

After migration:
- `src/punt_vox/voxd/music_handlers.py` → replaced by 6 files in `voxd/music/`
- `src/punt_vox/voxd/music_scheduler.py` → replaced by `voxd/music/scheduler.py`
- `src/punt_vox/voxd/track_generator.py` → replaced by `voxd/music/generator.py`
- `tests/test_voxd_music_handlers.py` → replaced by 6 files in `tests/music/`

### Execution order

1. Create `voxd/music/` package with `__init__.py` and `types.py` (`MusicResponse`, `MusicMode`, `MusicStatus`, `MusicState`)
2. Move `TrackGenerator` → `voxd/music/generator.py`, add `find_track()`. Write `tests/music/test_generator.py`.
3. Add domain methods to `MusicScheduler`, move to `voxd/music/scheduler.py`. **Leave writable setters in place** — handlers still use them. Write `tests/music/test_scheduler.py` (domain method tests).
4. Extract Method on `loop()`. Write `tests/music/test_scheduler_loop.py`.
5. One handler file + test file per handler (6 pairs). Each handler is thin — parse, delegate, catch `ValueError`, respond. **Remove writable setters from scheduler** — no longer needed now that handlers use domain methods.
6. Update `daemon.py` imports and wiring.
7. Update `voxd/__init__.py` re-exports.
8. Delete old files (`music_handlers.py`, `music_scheduler.py`, `track_generator.py`, `test_voxd_music_handlers.py`).
9. Run full test suite. All 1478+ tests pass.

Steps 1–3 are the design change. Step 4 is complexity reduction. Steps 5–8 are
mechanical migration. Each step is independently committable and testable.

**Setter removal timing (per review)**: Step 3 adds domain methods but leaves
setters in place so existing handlers continue to work. Step 5 rewrites handlers
to use domain methods and removes the now-dead setters. Every intermediate step
stays green. This follows PY-RF-1 (one transformation per step).

### Metrics after completion

| File | module_size | classes_per_module | max_complexity | All pass? |
|------|-------------|-------------------|----------------|-----------|
| `music/__init__.py` | ~40 | 0 | 0 | yes |
| `music/types.py` | ~25 | 1 | 1 | yes |
| `music/scheduler.py` | ~280 | 1 | ≤8 | yes |
| `music/generator.py` | ~120 | 1 | ≤5 | yes |
| `music/on_handler.py` | ~30 | 1 | ≤3 | yes |
| `music/off_handler.py` | ~15 | 1 | ≤2 | yes |
| `music/play_handler.py` | ~25 | 1 | ≤3 | yes |
| `music/list_handler.py` | ~15 | 1 | ≤2 | yes |
| `music/vibe_handler.py` | ~20 | 1 | ≤3 | yes |
| `music/next_handler.py` | ~15 | 1 | ≤2 | yes |

Zero metric failures across the music package.

### Non-negotiable constraints

- `from __future__ import annotations` in every file
- `__new__` constructors, never `__init__` (except `@dataclass`)
- All instance attributes prefixed with `_`
- `__slots__` on every class
- `__all__` in every module
- `@dataclass(frozen=True, slots=True)` for value objects
- `MessageHandler` protocol explicitly inherited by all handlers
- `module_size <= 300`, `classes_per_module <= 3`, `max_complexity <= 10`
- Tests mirror source structure: one test file per source module
- No `# noqa`, `# type: ignore`, `--no-verify`, or `xfail` added to pass checks
