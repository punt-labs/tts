# Design: the mpv program player

**Status:** Draft (design only — no implementation)
**Author:** gvr
**Created:** 2026-08-01
**Scope:** the daemon's PROGRAM audio tier (music today; audiobooks and podcasts
later). The notification tier (`afplay`/`say`/`espeak`) is out of scope and
untouched.

## Abstract

The program tier plays each part by spawning a fresh `ffplay` process and
killing it on pause, skip, or stop. Killing a process mid-buffer pops, and
resuming by re-spawning `ffplay` seeked to a wall-clock offset overlaps the
buffer and stutters — the operator has confirmed this by ear, and it is not
fixable within the spawn-and-kill model. This document replaces that model with
**one persistent `mpv` process** driven over its JSON IPC socket. Playing a part
becomes `loadfile`; ending a part becomes an `end-file` event; pause becomes
`set_property pause yes`, which mpv holds internally with no teardown and no
seek. The pause is click-free because nothing is torn down: mpv stops feeding
the audio output and resumes from the exact same decoder position.

The change swaps the *mechanism* of playback. It does not touch the *domain
state machine* — the `Program`/`SelectionPlayback` cursor and the transport
modes (`idle`/`playing`/`paused`) modelled in `docs/vox-music-player-transport.tex`
are unchanged. That model already anticipated this: it "fixes the behaviour and
the invariants, not the mechanism," and named the mechanism "the daemon
specialist's decision."

## Fixed rulings (design within these, do not relitigate)

- **Two audio tiers on purpose.** Notifications stay on the built-in
  `afplay`/`say`/`espeak` path (`voxd/playback.py`, the providers). Programs
  run on mpv. The two overlay at the OS mixer.
- **mpv is a HARD dependency.** No fallback, no `if mpv … else …`. Missing mpv
  is a loud, client-observable failure surfaced through status.
- **The `ffplay`-per-part path is deleted** (`programs/subprocess_player.py`).
  Forward integration, no shim (PL-PP-1). Note: `ffplay` survives *only* on the
  notification tier's Linux path (`voxd/playback.py`); it is removed from the
  program tier.

---

## 1. mpv process lifecycle

### When it is spawned — eager, at daemon bring-up

mpv is spawned when the program subsystem comes up in the daemon lifespan
(`VoxDaemon._lifespan`, alongside the control writer and playback loop), not
lazily at the first program. Rationale:

- mpv is a persistent daemon-owned resource, like the audio device the daemon
  already owns. An `--idle` mpv with no file loaded holds no audio output and
  costs almost nothing.
- Eager spawn gives one place to detect "mpv missing" and raise it as a standing
  fault before any program command runs. Missing-at-startup and
  missing-at-first-program collapse into one code path and one fault surface.

**Spawn failure does NOT crash the daemon.** The notification tier is
independent and must keep working. A spawn failure records a standing
`PLAYER_UNAVAILABLE` fault (see §4) that `ProgramStatus.playback_error` surfaces
and that every program command reflects. This is the "loud, client-observable,
no fallback" ruling realised without taking down notifications.

### Who owns it

A new `MpvSupervisor` owns the process-and-connection lifecycle across restarts;
a new `MpvClient` owns one live connection (process handle + socket + reader
task + request correlation). `ProgramService` composes the supervisor (wired in
`ProgramSubsystem`, `programs/wiring.py`) and hands the derived
`MpvProgramPlayer` to the `ProgramLoop`, replacing the injected
`SubprocessPlayer`.

### Teardown on daemon shutdown

`ProgramService.shutdown()` (already called in the lifespan `finally`) also stops
the supervisor: send the `quit` IPC command for a graceful exit, then a
synchronous `SIGKILL` fallback if the process does not exit promptly (mirroring
today's `SubprocessHandle.terminate`), close the socket, and cancel the reader
task. Shutdown runs outside the event loop's normal flow, so the fallback kill is
synchronous, exactly as `PlaybackSuspension.shutdown` is today.

### If mpv CRASHES mid-playback — restart, reload current part from start

Detection: the reader task sees socket EOF (`readline()` returns `b""`), or the
process `returncode` is set. Either is a crash signal to the supervisor.

Recommendation: **restart-and-reload-current-part-from-start**, not
surface-an-error-only.

- The supervisor records a standing `PLAYER_CRASH` fault (client-observable),
  respawns mpv, reconnects the socket, and reloads the daemon's *current* part —
  from **offset 0**, not mid-track.
- Reloading from the start is deliberate. mpv's crash loses its internal
  position, and recovering it would require continuously polling
  `get_property time-pos` — which reintroduces exactly the wall-clock
  position-tracking this design deletes (§3). A crash is rare and exceptional;
  restarting the current part from the top is acceptable and observable.
- A **bounded backoff** between restarts (reuse the existing
  `_SPAWN_BACKOFF_SECONDS = 2.0` idiom) and a **restart cap** within a window:
  if mpv crashes repeatedly, stop restarting, enter a `PLAYER_FAILED` standing
  fault, and surface it. This prevents a hot respawn loop, mirroring the loop's
  existing spawn-backoff + `PlaybackHealth` pattern.

### Startup flags

```
mpv --idle=yes \
    --no-video --vo=null \
    --input-ipc-server=<run_dir>/mpv.sock \
    --no-config \
    --volume=30 \
    --gapless-audio=yes \
    --terminal=no --msg-level=all=warn
```

- `--idle=yes` — stay alive with no file loaded (the persistent process).
- `--no-video --vo=null` — audio only.
- `--input-ipc-server=<path>` — the JSON IPC socket, a per-daemon unix socket
  under the run dir.
- `--no-config` — ignore `~/.config/mpv`; reproducible behaviour, no
  user keybindings, scripts, or profiles leaking in.
- `--volume=30` — the reduced music volume that replaces `ffplay -volume 30`, so
  speech and chimes overlay it (§5). mpv volume is 0–100.
- `--gapless-audio=yes` — mpv default; stated for intent.
- `--terminal=no --msg-level=all=warn` — quiet, non-interactive.

---

## 2. The IPC protocol layer

### Transport and framing

A unix domain socket at `--input-ipc-server`, connected with
`asyncio.open_unix_connection`. Commands are newline-delimited JSON; responses
and events are newline-delimited JSON.

### Command set

| Command | JSON | Purpose |
|---------|------|---------|
| loadfile | `{"command": ["loadfile", <path>, "replace"], "request_id": N}` | Play a part (§3). A per-file `{"pause": "yes"}` option loads it paused for prev/next-while-paused (Fork B). |
| pause | `{"command": ["set_property", "pause", true], "request_id": N}` | Click-free suspend — the whole point. |
| resume | `{"command": ["set_property", "pause", false], "request_id": N}` | Click-free continue. |
| stop | `{"command": ["stop"], "request_id": N}` | Unload the current file, return mpv to idle (off / interrupt teardown). Emits `end-file` reason `stop`. |
| quit | `{"command": ["quit"], "request_id": N}` | Graceful shutdown. |

`get_property time-pos`/`duration` are intentionally **not** in the working set.
The cursor is part-level, not second-level; mpv owns intra-track position. They
stay available only if a future "seconds into track" status field is wanted.

### Request/response correlation

A monotonic `request_id` counter and a `dict[int, asyncio.Future]`. The command
coroutine registers a future, writes the framed command, and awaits the future
**with a timeout** (a wedged-but-alive mpv must surface as a fault, never hang
the loop). The reader task resolves the future when a response bearing that
`request_id` arrives. `MpvClient` serialises its own writes (one send lock or an
internal send queue), because three callers write concurrently: the loop
(`loadfile`), the suspension (`pause`/`resume`), and shutdown (`quit`).

### The async event stream

mpv pushes unsolicited events with no `request_id`. The reader classifies each
line: a `request_id` present → resolve a pending future; an `"event"` key
present → dispatch to the event handler; anything else → log and skip (a
malformed line never kills the reader).

The one event that drives the state machine is **`end-file`**, discriminated by
its `reason`:

| `reason` | Meaning | Loop action |
|----------|---------|-------------|
| `eof` | natural end of the loaded part | advance (post `Rotate`) — this is Z `AutoAdvance` |
| `stop` / `redirect` / `quit` | deliberate teardown or replace | do NOT advance |
| `error` | bad/corrupt file | record a per-part fault (F3), advance past it |

This is the exact analog of today's `TrackEnd`: `eof` ↔ clean exit code 0,
`error` ↔ non-zero exit, `stop`/`redirect` ↔ user-interrupt. `TrackEnd` is
re-expressed over an `EndFileReason` enum instead of a process exit code.

### Connect and reconnect

After spawn the socket may not exist yet; the connect routine retries with a
short bounded loop until the socket accepts or a timeout elapses (then a
`PLAYER_UNAVAILABLE` fault). mpv restart (§1) reuses the identical connect
routine, so "first connect" and "reconnect after crash" are one path.

---

## 3. Mapping onto the existing state machine

The domain state machine (`ProgramState`/`SelectionPlayback` cursor,
`idle`/`playing`/`paused`) is unchanged. What changes is the *player mechanism*
the loop drives. The `ControlChannel` single-writer, the `ChangeSignal`, and the
lux receive leg are all unchanged.

### The loop: `spawn → proc.wait()` becomes `loadfile → await end-file`

`ProgramLoop._play(target)` today spawns a process and awaits `proc.wait()`.
It becomes: `loadfile target` (paused per the suspension flag — see prev/next),
then await the load's **ended-future**, which the reader resolves when an
`end-file` event arrives for the current load. `InterruptRace` is reused almost
verbatim: it still races an ended-future against the `interrupt` Event; only the
future's source changes (an mpv event, not `proc.wait()`). `_finish` reads the
`EndFileReason` instead of an exit code.

`_wait_for_playable` (pool empty in `generating-first`) is unchanged: mpv sits
idle with no file; the loop blocks on `channel.changed` until a part is
playable.

### Per-operation mapping

| Operation | Today | With mpv |
|-----------|-------|----------|
| **play / switch** (`PlayAlbum`, `SwitchProgram`, `SwitchSelection`) | control signal moves source, interrupts loop, loop spawns part 1 | same signal + interrupt; loop `loadfile` part 1 (playing); suspension reset drops any pause. **T1** holds — one mpv, one loaded file. |
| **auto-advance** (`AutoAdvance` → `Rotate`) | `proc.wait()` returns 0, loop posts `Rotate`, spawns next | `end-file` reason `eof`, loop posts `Rotate`, `loadfile` next. **T3** holds: a paused mpv emits no `eof`, so `Rotate` cannot fire while paused. |
| **pause** (`Pause`) | tear player down (SIGTERM), record wall-clock `ResumePoint` | `set_property pause true`; set paused flag; emit change. mpv freezes gapless. Loop is unaffected — still awaiting an `eof` that will not come. |
| **resume** (`Resume`) | re-spawn player seeked to the frozen offset | `set_property pause false`; clear paused flag; emit change. mpv continues from the exact decoder position — click-free, no reload, no seek. |
| **prev / next** (`Prev`, `Next`) | move cursor, interrupt loop, loop re-spawns seeked-or-fresh | move cursor, interrupt loop; loop `loadfile` the new part, **with `pause=yes` when the suspension flag is set** (Fork B — stays paused, new part from offset 0). |
| **stop / off** (`Stop`, `TurnOff`) | control signal → idle, loop kills player | control signal → idle; loop sends `stop`; mpv returns to idle. Suspension reset. **T1/T2** idle shape. |

### Why T1–T7 still hold

The transport invariants are properties of the *source* state, which mpv does
not touch:

- **T1** (single active source): one mpv, at most one loaded file.
- **T2** (now-playing iff active): the cursor lives in `Program`/`SelectionPlayback`, unchanged.
- **T3** (paused is suspended): guaranteed *for free* — a paused mpv reaches no
  `eof`, so `AutoAdvance`/`Rotate` cannot fire. Today this needs an explicit loop
  gate; mpv makes it intrinsic.
- **T4** (transition guards): unchanged — the guards are on the control signals,
  not the player.
- **T5** (cursor bounds): source-level, unchanged.
- **T6** (glyph reflects state): derived from mode, unchanged.
- **T7** (catalogued): source-level, unchanged.

### What `suspension.py` becomes

`PlaybackSuspension` sheds almost everything. mpv owns the position, so the
wall-clock machinery goes:

- **Deleted:** `resume_point.py` in full (`LiveTrack`, `ResumePoint` — the
  offset/clock accounting). `seek_for`, `attach`, `detach`, the live handle, the
  gate (`wait_resumed`).
- **Kept:** the `paused` flag (the one authoritative place status reads
  `is_paused`), and `pause()`/`resume()`/`reset()` — now thin: they call
  `player.pause()`/`player.resume()` (the IPC) and flip the flag. `reset()`
  (switch/off) clears the flag; the loop's `stop`/new `loadfile` handles the mpv
  side.

The loop reads `suspension.is_paused` at `loadfile` time only, to decide whether
a prev/next reload loads paused (Fork B). The gate that parked the loop is gone —
mpv's internal pause *is* the suspension.

### Composition with the single-writer and the lux receive leg

- The `ControlChannel` single-writer is untouched. It still serialises source
  mutations; the loop still posts `Rotate` through it; the reader's `eof` event
  enters the state machine only via that `Rotate`, never directly.
- mpv events do **not** flow into lux. They flow into the loop (ended-future) or
  the supervisor (crash). Scene re-push still rides `ChangeSignal.emit()` after
  each applied command, exactly as today. One change path, preserved.
- A new background actor appears: the `MpvClient` reader task. It is owned by the
  client (started on connect, cancelled on crash/shutdown), not one of the
  lifespan's explicit tasks — keeping the daemon's task list stable.

### One concurrency question (open — see §8)

`service.pause()`/`resume()` are today *direct* calls, not routed through the
`ControlChannel`. With mpv they become IPC calls that can race an `eof`-driven
`Rotate`. The race is benign (pause lands on part N or, if `eof` won first, on
the freshly-loaded N+1), and `MpvClient` serialises its own sends regardless.
The recommendation is to keep pause/resume as direct calls; routing them through
the channel is the alternative. Flagged for a ruling in §8.

---

## 4. Failure and error surfacing

Every failure is client-observable through `ProgramStatus.playback_error`
(already the surface for spawn/track faults) — never silent, never a log-only
signal ("Reading a log is never a client interface"), never a fallback. Raw
detail (absolute paths, exception text) goes to `vox.log`; the wire reason is
`SafeText`-sanitised, exactly as today.

Extend `PlaybackFaultKind` (`types_programs/playback_fault.py`) with mpv kinds:

| Situation | Detection | Surfaced as |
|-----------|-----------|-------------|
| mpv missing at startup | spawn raises `FileNotFoundError` | standing `PLAYER_UNAVAILABLE` fault; daemon stays up (notifications unaffected); every program command reflects it |
| mpv missing at first program | (does not occur — eager spawn) collapses to the startup case | `PLAYER_UNAVAILABLE` |
| mpv crash mid-playback | socket EOF / process exit | `PLAYER_CRASH`; restart + reload from start (§1); `PLAYER_FAILED` if the restart cap is hit |
| IPC write failure (`BrokenPipeError` on send) | send raises | treated as a crash signal → restart path |
| IPC read failure / wedged mpv | command future times out | fault + backoff; the loop treats a timed-out `loadfile` like today's spawn failure (record fault, back off), never a silent hang |
| bad/corrupt part file | `end-file` reason `error` | per-part fault (F3), advance past it — the existing `failed_parts` surface |

The existing per-part faults (`SPAWN`, `TRACK_EXIT`) collapse: there is no
per-part spawn, so `SPAWN` retires; `TRACK_EXIT` becomes the `end-file` `error`
case. The new kinds describe the *one process's* lifecycle rather than a
per-part process.

---

## 5. The two-tier boundary

Exactly which code is which:

| Tier | Backend | Code | Volume |
|------|---------|------|--------|
| **Programs** (music, later audiobooks/podcasts) | one persistent **mpv** + JSON IPC | `programs/` — the loop, `MpvSupervisor`, `MpvClient`, `MpvProgramPlayer` | reduced (`--volume=30`) |
| **Notifications** (speech + chimes) | **afplay** (macOS) / **ffplay** (Linux), `say`/`espeak` | `voxd/playback.py` (`PlaybackQueue._player_command`), `providers/say.py`, `providers/espeak.py` | full |

The notification tier is untouched by this change. `ffplay` is removed from the
*program* tier only; it remains the notification tier's Linux player.

### They overlay — two OS-level streams

A chime over ducked music is **two independent processes** writing to the shared
system output: mpv (music, ducked to 30%) and afplay (chime, full). The OS mixer
sums them — CoreAudio on macOS, PulseAudio/PipeWire on Linux — exactly as
ffplay-music + afplay-chime do today. No cross-tier coordination and no shared
handle. The duck is **static** (a constant reduced mpv volume), as it is today.

Note (out of scope, enabled by this change): mpv makes *dynamic* ducking trivial
— `set_property volume` down while speech plays, up after — which the
spawn-per-part model could not do. A future enhancement, not this design.

---

## 6. Does the mpv process/connection lifecycle warrant its own Z model?

**Yes.** The player *state* (idle/playing/paused) is already modelled in
`docs/vox-music-player-transport.tex` and is unchanged. But the mpv
*process/connection lifecycle* is new stateful behaviour, and it meets every
trigger in the WORKFLOW.md z-spec gate: 3+ states with transitions, invariants
that must hold across transitions, and a silent-corruption failure mode (a
command sent to a dead socket, a double-spawn on a restart race, an unbounded
respawn loop). It is directly analogous to the LuxListener connection lifecycle
that warranted its own model.

Recommendation: author `docs/mpv-program-player.tex`, `fuzz`-clean and
ProB-model-checked, **in the design phase, before the implementation mission
dispatches.** Sketch:

### States

```
MpvState ::= down | starting | ready | crashed | restarting | failed
```

- `down` — no process (before spawn, after shutdown).
- `starting` — process spawned, socket not yet connected.
- `ready` — process alive, socket connected, commands accepted.
- `crashed` — process/socket died, a restart is owed.
- `restarting` — respawning within the cap.
- `failed` — restart cap exceeded; given up; standing hard fault.

### State schema (sketch)

```
MpvLifecycle
  state     : MpvState
  processes : 0 .. 1          -- live mpv processes
  readers   : 0 .. 1          -- live reader tasks
  restarts  : 0 .. maxRestarts
  fault     : ZBOOL           -- a standing client-observable fault
  ----------------------------------------------------------------
  (state = ready  ⟺ processes = 1 ∧ readers = 1)   -- I1: ready ⟺ connected
  (state ∈ {down, failed} ⟹ processes = 0 ∧ readers = 0)
  processes ≤ 1                                     -- I2: never double-spawn
  readers   ≤ 1                                     -- I5: one reader
  (fault = ztrue ⟺ state ∈ {crashed, restarting, failed})  -- I3
  restarts ≤ maxRestarts                            -- I4
  (state = failed ⟹ restarts = maxRestarts)
```

### Invariants to prove

- **I1 — commands only when ready.** A `loadfile`/`pause`/`stop` is enabled only
  in `ready`; issued in any other state it is refused into a fault, never
  silently dropped.
- **I2 — at most one process.** No restart race spawns a second mpv.
- **I3 — fault ⟺ not ready.** A client sees a standing fault in exactly the
  non-ready states.
- **I4 — the restart cap terminates.** `restarts` is monotone and bounded;
  reaching the cap ⟹ `failed` with no further spawn (no hot loop).
- **I5 — one reader.** The reader is alive iff `ready`; started on connect,
  cancelled on crash/shutdown.
- **I6 — the process lifecycle does not corrupt source state.** A crash/restart
  leaves the daemon cursor untouched; reload targets the same current part.

### Transitions to model

`Spawn` (down→starting), `Connect` (starting→ready), `SendWhenReady`,
`CrashDetected` (ready→crashed), `Restart` (crashed→starting if
`restarts < maxRestarts` else crashed→failed), `Shutdown` (any→down via quit).

### What ProB should exhaust

No reachable state sends a command while not `ready` (I1); the restart cap
terminates (no infinite respawn, I4); `crashed` always leads to `ready` again or
`failed` (no wedge); at most one process and one reader throughout (I2, I5).

The `.tex` is a design-mission deliverable; implementation does not dispatch
until it is `fuzz`-clean and its findings are resolved.

---

## 7. Decomposition / write-set

### Add

| Module | Responsibility |
|--------|---------------|
| `programs/mpv/mpv_client.py` | `MpvClient` — one live connection: process handle, socket, reader task, request/response correlation, self-serialised sends. `send(command) -> response`, an event subscription, `is_ready`. |
| `programs/mpv/mpv_supervisor.py` | `MpvSupervisor` — spawn / connect / crash-detect / restart-with-backoff-and-cap across connections; owns the standing mpv fault surface. |
| `programs/mpv/mpv_program_player.py` | `MpvProgramPlayer` — the loop-facing player: `play(part) -> handle` (`loadfile` + an ended-future resolved by `end-file`), `stop()`, `pause()`, `resume()`. |
| `types_programs/mpv_event.py` | `MpvEvent`, `EndFileReason` enum (`eof`/`stop`/`redirect`/`quit`/`error`), `MpvCommand`/`MpvResponse` value types (PY-IC-9: types in their own module). |

The design mission decides the final split (one `mpv/` package vs. flatter),
per the "design mission's output IS the write-set" rule. The above is the shape,
not a mandate.

### Delete

- `programs/subprocess_player.py` — the `ffplay`-per-part `SubprocessPlayer` /
  `SubprocessHandle`.
- `programs/resume_point.py` — `LiveTrack`, `ResumePoint`. mpv owns position.

### Modify

- `programs/loop.py` — `_play`: `loadfile` (paused per flag) + await ended-future;
  `_finish`: read `EndFileReason` not an exit code; drop the offset/seek path.
- `programs/suspension.py` — slim to the paused flag + `pause`/`resume`/`reset`
  delegating to the player; drop the gate, the live handle, and the resume point.
- `programs/player.py` — rework the protocol: `play(part) -> PlayHandle` (handle
  exposes an ended-future carrying the `EndFileReason`); session-level `stop()`,
  `pause()`, `resume()`; drop per-handle `stop_gracefully`/`terminate` and the
  `offset` parameter.
- `programs/interrupt_race.py` / `programs/track_end.py` — re-express `TrackEnd`
  over `EndFileReason`; the race structure is otherwise reusable.
- `programs/service.py` — compose the supervisor/player; `shutdown()` also quits
  mpv; `pause`/`resume` delegate through the slim suspension.
- `programs/wiring.py` (`ProgramSubsystem`) — build the `MpvSupervisor`, inject
  the derived player into the service.
- `voxd/daemon.py` (`_lifespan`) — bring the supervisor up during program-subsystem
  start; the reader task lives inside `MpvClient`; shutdown quits mpv.
- `types_programs/playback_fault.py` — extend `PlaybackFaultKind`
  (`PLAYER_UNAVAILABLE`, `PLAYER_CRASH`, `PLAYER_FAILED`); retire `SPAWN`, fold
  `TRACK_EXIT` into the `end-file` `error` case.

### Testing seam

Unit tests cannot spawn real mpv in CI. The injection seam is the
`Player`/`MpvClient` boundary (as `SubprocessPlayer` is injected today): a
`FakeMpvClient` records commands and lets a test inject `end-file` events and
crashes. Per-event dispatch, the malformed-line path, the timeout path, and the
crash/restart path are all tested against the fake.

---

## 8. Open questions for the leader

1. **mpv crash recovery: reload from start vs. surface-and-stop.**
   Recommendation: restart + reload the current part from **offset 0**, with a
   bounded backoff and a restart cap (§1). Rejected alternative: recover the
   exact position, which requires continuous `time-pos` polling and reintroduces
   the wall-clock machinery this design deletes. Confirm or rule otherwise.

2. **Do pause/resume route through the `ControlChannel` single-writer?**
   Recommendation: keep them as **direct** IPC calls (as today), relying on
   `MpvClient`'s own send-serialisation; the pause-vs-`eof` race is benign.
   Alternative: route them through the channel as control signals so every
   mpv-affecting operation serialises on one writer (model-literal, but pause
   then touches the player from the writer thread as well as the loop). A real
   concurrency-model decision.

3. **`--volume=30` static duck vs. a config knob.** The current design hard-codes
   the reduced music volume (matching today's `_MUSIC_VOLUME = 30`).
   Recommendation: keep it a constant for this change; dynamic ducking and a
   user knob are separate follow-ups (§5). Confirm the constant is acceptable
   for now.

4. **Eager spawn without crashing the daemon.** Recommendation (§1): spawn mpv at
   bring-up; a spawn failure records a standing `PLAYER_UNAVAILABLE` fault but
   leaves the daemon (and the notification tier) running. Rejected alternative:
   fail the whole daemon on missing mpv — it would take down the independent
   notification tier. Confirm.
