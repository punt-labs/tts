# MusicScheduler Split — `scheduler.py` → `scheduler.py` + `loop.py`

Date: 2026-05-14
Author: claude (COO)
Reviewer: Ralph Johnson (rej)
Status: GO — revised per review feedback

## Problem

`voxd/music/scheduler.py` is 650 lines with two distinct responsibilities in
one class:

1. **Domain operations** (lines 150–262) — `turn_on`, `turn_off`, `play_track`,
   `update_vibe`, `skip_next`. Request/response. Synchronous state transitions
   that validate inputs and return `MusicResponse`.

2. **Async playback coordination** (lines 275–651) — `loop`,
   `_playback_wait_loop`, `_run_initial_generation`,
   `_handle_generation_complete`, `_handle_vibe_change`, `_generate_track`,
   `_backoff_sleep`. Autonomous background lifecycle that races subprocesses
   against events, manages retry/backoff, and coordinates gapless handoff.

These are two reasons to change (PY-IC-6 Single Responsibility). A bug in vibe
ownership logic is in (1). A bug in gapless handoff timing is in (2). They
share state but operate at different abstraction levels.

The module also exceeds `module_size <= 300` (650 vs 300 limit).

## Proposed Design

### `scheduler.py` (~250 lines) — domain operations + state ownership

Keeps:
- All state fields (`_mode`, `_style`, `_owner`, `_vibe`, `_track`,
  `_track_name`, `_proc`, `_state`, `_changed`, `_replay`)
- All read-only properties
- Domain methods: `turn_on`, `turn_off`, `play_track`, `update_vibe`, `skip_next`
- `_kill_proc` (private, manages subprocess lifecycle)
- `kill_proc` (public wrapper, used by daemon lifespan shutdown)
- `_generate_track` (thin wrapper around TrackGenerator — reads/writes
  scheduler fields `_vibe`, `_style`, `_track_name`)

Removes: `loop`, `_playback_wait_loop`, `_run_initial_generation`,
`_handle_generation_complete`, `_handle_vibe_change`, `_backoff_sleep`,
`_PlaybackWaitResult`, `_MUSIC_MAX_RETRIES`.

Adds: `shutdown()` async method that kills proc and sets state to idle. Replaces
the daemon lifespan's direct `kill_proc` + state manipulation.

### `loop.py` (~350 lines) — async playback coordination

New class: `MusicLoop`.

```python
class MusicLoop:
    __slots__ = ("_scheduler",)

    def __new__(cls, scheduler: MusicScheduler) -> Self:
        self = super().__new__(cls)
        self._scheduler = scheduler
        return self

    async def run(self) -> None:
        """Background task: generate and loop music tracks."""
        ...
```

Moves here:
- `run` (renamed from `loop` — the method name was confusing on a class called
  `MusicScheduler`; on `MusicLoop` it's the natural name)
- `_playback_wait_loop`
- `_run_initial_generation`
- `_handle_generation_complete`
- `_handle_vibe_change`
- `_backoff_sleep`
- `_PlaybackWaitResult` (private dataclass, stays in same module as its consumer)
- `_MUSIC_MAX_RETRIES` (constant, used only by loop logic)

### How MusicLoop accesses scheduler state

`MusicLoop` holds a reference to `MusicScheduler` and reads state through
read-only properties: `mode`, `vibe`, `track`, `replay`, `state`, `changed`.

For the small number of writes the loop must make, the scheduler exposes
**intent-revealing state transition methods** — not individual field setters.
Each method writes correlated fields atomically and enforces invariants. The
loop announces intent; the scheduler decides what that means for its state.

```python
def begin_generation(self) -> None:
    """Loop is starting a generation pass."""
    self._state = "generating"

def complete_generation(self, track: Path) -> None:
    """Loop finished generating; record the track."""
    self._track = track
    self._track_name = track.stem

def begin_playback(self, proc: asyncio.subprocess.Process) -> None:
    """Loop started a playback subprocess."""
    self._proc = proc
    self._state = "playing"

def disable(self) -> None:
    """Loop exhausted retries; disable music."""
    self._mode = "off"
    self._state = "idle"

def consume_replay(self) -> Path:
    """Loop is consuming a replay directive. Clears flag, returns track."""
    self._replay = False
    if self._track is None:
        raise RuntimeError("music_replay set but music_track is None")
    return self._track

async def shutdown(self) -> None:
    """Daemon lifespan cleanup. Kills proc, resets state."""
    await self._kill_proc()
    self._state = "idle"
```

Each method is a Command (PY-DP pattern) at the method boundary. No sequence-
dependent field writes from outside — the scheduler owns the invariants.

All mutating methods return `None` (PY-OP-8). `consume_replay` returns `Path`
because it is a query+mutation (consume pattern), not a pure mutator.

`_changed` — the loop calls `clear()` and `wait()` on the event object exposed
via the read-only `changed` property. Domain methods are the sole callers of
`set()`. This is a **single-producer (domain methods), single-consumer (loop)**
event channel. `MusicLoop` is the only class that calls `clear()` and `wait()`.
This invariant must be preserved — a second consumer calling `clear()` would
cause lost wakeups.

### `generate_track` stays on MusicScheduler (public)

`generate_track` is a Facade (PY-DP-10 at the method level) over scheduler
state. It reads `_vibe`, `_style`, `_track_name` and writes back `_track_name`
after generation. The loop should not know about these fields — it should only
know that it can request "generate a track from current state" and get a `Path`
back. Keeping `generate_track` on the scheduler preserves this information
hiding.

The method is **public** (`generate_track`, not `_generate_track`). It is called
by `MusicLoop` which is a distinct class in a distinct module. Private cross-
module access with pyright suppression violates PY-EN-2. The method name is
intention-revealing and the public surface is narrow.

### Dependency direction

```
MusicLoop ──depends on──> MusicScheduler ──depends on──> TrackGenerator
```

`MusicScheduler` does not know `MusicLoop` exists. The daemon creates both and
starts the loop:

```python
scheduler = MusicScheduler(track_generator)
music_loop = MusicLoop(scheduler)
loop_task = asyncio.create_task(music_loop.run())
```

This replaces the current pattern where the daemon calls `scheduler.loop()`
directly. The `loop_task` property on `MusicScheduler` can be removed — the
daemon holds the task reference directly.

### Module layout after split

| File | Class | Lines (est.) | Responsibility |
|------|-------|-------------|----------------|
| `scheduler.py` | `MusicScheduler` | ~250 | State ownership, domain methods, subprocess kill, track generation |
| `loop.py` | `MusicLoop`, `_PlaybackWaitResult` | ~350 | Async coordination: event racing, gapless handoff, retry/backoff |

Both under 300? `loop.py` at ~350 is over. The `_playback_wait_loop` method
alone is ~110 lines. Options:

(a) Accept ~350 for `loop.py` — it's one class with genuinely complex async
coordination. Splitting further would separate the wait-loop from the outer
loop, which are tightly coupled.

(b) Extract `_playback_wait_loop` into its own class `PlaybackWaiter` in a
separate module. This is feasible — the wait loop takes a proc, track, gen_task,
and retry_count, and returns a `_PlaybackWaitResult`. It's a self-contained
unit. But it would need access to the scheduler's `_kill_proc`, `_generate_track`,
and state properties, which means it also takes a scheduler reference.

(c) Reduce `loop.py` to under 300 by inlining the `_PlaybackWaitResult`
dataclass fields directly into the return of `_playback_wait_loop` (e.g.,
returning a tuple). This saves ~15 lines but hurts readability.

**Recommendation**: option (a). `loop.py` at ~350 lines with one class is a
marginal violation. The complexity is inherent — async event racing with gapless
handoff, retry, and cancellation is genuinely complex. Splitting further would
create two modules that both depend on the same scheduler state and neither can
be understood independently — coupling without cohesion. PY-OO-2 says "likely
violates SRP and should be split" — the key word is "likely." A single class
performing one coherent responsibility at 350 lines, with no external consumers
and no prospect of reuse, is the exception the rule allows. Document the
exception in the module docstring.

### Daemon wiring changes

In `daemon.py`:

```python
from punt_vox.voxd.music.loop import MusicLoop

# In _build_handler_dict or create_app:
scheduler = MusicScheduler(track_generator)
music_loop = MusicLoop(scheduler)

# In lifespan:
loop_task = asyncio.create_task(music_loop.run())
```

Remove `scheduler.loop_task` property and the `loop_task` setter (if it still
exists). The daemon owns the task reference.

### Test changes

| Test file | Changes |
|-----------|---------|
| `tests/music/test_scheduler.py` | No change — domain method tests don't touch the loop |
| `tests/music/test_scheduler_loop.py` | Rename to `tests/music/test_loop.py`. Construct `MusicLoop(scheduler)` instead of calling `scheduler.loop()`. |
| `tests/test_voxd_music.py` | Update imports if it references `scheduler.loop` |

New tests for intent methods (in `tests/music/test_scheduler.py`):
- `test_begin_generation_sets_state`
- `test_complete_generation_sets_track_and_name`
- `test_begin_playback_sets_proc_and_state`
- `test_disable_sets_mode_off_and_state_idle`
- `test_consume_replay_returns_track_and_clears_flag`
- `test_consume_replay_raises_when_no_track`
- `test_shutdown_kills_proc_and_resets_state`

### Execution steps

1. Add intent methods to `MusicScheduler` (`begin_generation`,
   `complete_generation`, `begin_playback`, `disable`, `consume_replay`,
   `shutdown`). Make `generate_track` public. Add tests.
2. Create `loop.py` with `MusicLoop` class. Move `loop` → `run`, move all
   private loop helpers, `_PlaybackWaitResult`, `_MUSIC_MAX_RETRIES`. Replace
   direct `self._field` access with `self._scheduler.property` reads and
   `self._scheduler.method()` calls.
3. Remove loop methods from `scheduler.py`.
4. Update `daemon.py` wiring: create `MusicLoop(scheduler)`, start
   `music_loop.run()` as background task, call `scheduler.shutdown()` in
   lifespan cleanup. Remove `loop_task` property from scheduler.
5. Update `voxd/music/__init__.py` re-exports (add `MusicLoop`).
6. Update tests: rename `test_scheduler_loop.py` → `test_loop.py`, construct
   `MusicLoop(scheduler)` instead of `scheduler.loop()`. **Update patch
   targets**: `punt_vox.voxd.music.scheduler.asyncio` →
   `punt_vox.voxd.music.loop.asyncio`.
7. Run full test suite.

### Non-negotiable constraints

- `from __future__ import annotations` in every file
- `__new__` constructors, never `__init__` (except `@dataclass`)
- All instance attributes prefixed with `_`
- `__slots__` on every class
- `__all__` in every module
- `@dataclass(frozen=True, slots=True)` for value objects
- `module_size <= 300`, `classes_per_module <= 3`, `max_complexity <= 10`
- No `# noqa`, `# type: ignore`, `--no-verify`, or `xfail` added to pass checks
- Tests mirror source structure
