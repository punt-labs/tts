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

### Reaping an orphan left by an unclean prior exit — by socket, not by pid

mpv is spawned `start_new_session=True` with `--idle=yes`, so a SIGKILL, OOM, or
crash of the daemon *before* teardown runs leaves an idle-forever mpv holding our
IPC socket. Without cleanup the next bring-up would spawn a *second* mpv, breaking
I2 across restarts. `OrphanReaper` (`programs/mpv/orphan_reaper.py`) closes that
gap once, before the first spawn.

The identity it reaps by is the **socket**, not a recorded pid. Only our mpv can
own our `--input-ipc-server` socket path, so a listening socket is a safe target:
the reaper connects and sends mpv's `quit` command — a clean shutdown of exactly
the process that owns *our* socket — then unlinks the stale path so the fresh mpv
binds a new inode. A path that exists but no longer listens (a stale inode) is
simply unlinked; there is nothing to quit.

There is deliberately **no kill-by-pid fallback**. The IPC socket lives under a
persistent run dir that survives a reboot, and a recorded pid can be recycled onto
an unrelated process; SIGKILLing it would kill the wrong target on the ordinary
give-up path, not just a rare race. A wedged mpv that ignores `quit` is rare — it
is logged and left, which is safer than ever risking the wrong process. Every
socket operation (probe, send, unlink) is robust to `OSError`, and the reap runs
inside the supervisor's fault guard, so a probe error stands a fault rather than
escaping into a silent bring-up hang.

### Who owns it

A new `MpvSupervisor` owns the process-and-connection lifecycle across restarts —
spawn, connect, crash-detect, restart — and **nothing about what plays**. It
never issues `loadfile`; that is the loop's alone (the single-loadfile-ownership
invariant, §6). A new `MpvClient` owns one live connection (process handle +
socket + reader task + request correlation). `ProgramService` composes the
supervisor (wired in `ProgramSubsystem`, `programs/wiring.py`) and hands the
derived `MpvProgramPlayer` to the `ProgramLoop`, replacing the injected
`SubprocessPlayer`.

### Teardown on daemon shutdown

`ProgramService.shutdown()` (already called in the lifespan `finally`) also stops
the supervisor: send the `quit` IPC command for a graceful exit, then a
synchronous `SIGKILL` fallback if the process does not exit promptly (mirroring
today's `SubprocessHandle.terminate`), close the socket, and cancel the reader
task. Shutdown runs outside the event loop's normal flow, so the fallback kill is
synchronous, exactly as `PlaybackSuspension.shutdown` is today.

### If mpv CRASHES mid-playback — the supervisor restarts the process; the loop replays the current part

Detection: the reader task sees socket EOF (`readline()` returns `b""`), or the
process `returncode` is set. Either is a crash signal to the supervisor.

**The supervisor restarts the *process*; the loop owns every `loadfile`.** This
is a settled division of labour (peer-reviewed, gvr + kpz). The supervisor's job
is spawn / connect / crash-detect / restart of the mpv process — nothing more. It
never issues `loadfile`. Restoring playback after a crash belongs to the loop,
which already owns the play-a-part decision and holds the cursor. Splitting it
the other way — the supervisor reloading — is what the earlier draft did, and it
allowed a crash→recover transition nothing listened to: a double-load (the
supervisor's reload racing the loop's next `loadfile`) and a loop wedged forever
on an ended-future that a crash never resolves.

The sequence:

1. **Reader detects the crash and unblocks everyone.** On socket EOF the reader
   resolves the loop's in-flight ended-future with a **non-advancing reason,
   `crashed`**, and fails every other pending command future (§2). A crash emits
   no `end-file`, so without this the loop's `await` would never return. The
   reader also signals the supervisor.
2. **The supervisor restarts the process.** It records a standing `PLAYER_CRASH`
   fault (client-observable), respawns mpv, and reconnects the socket — reaching
   `ready` again — with a **bounded backoff** (reuse the existing
   `_SPAWN_BACKOFF_SECONDS = 2.0` idiom) and a **restart cap** within a window. If
   mpv crashes repeatedly, it stops restarting, enters a `PLAYER_FAILED` standing
   fault, and surfaces it. This prevents a hot respawn loop, mirroring the loop's
   existing spawn-backoff + `PlaybackHealth` pattern.
3. **The loop replays the current part.** Seeing the `crashed` reason (a
   non-advancing outcome — the cursor does not move), the loop waits for mpv to
   reach `ready` again (§3, the `WaitReady` step) and re-plays the **current**
   part — cursor unmoved — from **offset 0**. The reload honours the paused flag:
   if `is_paused`, the loop reloads with `pause=yes`, so a crash while paused does
   not silently start playing while status still reports paused.

Reloading from offset 0, not the exact pre-crash position, is deliberate and
settled. mpv's crash loses its internal position, and recovering it would require
continuously polling `get_property time-pos` — which reintroduces exactly the
wall-clock position-tracking this design deletes (§3). A crash is rare and
exceptional; restarting the current part from the top is acceptable and
observable.

### Startup flags

```bash
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

**Pin the mpv version.** The IPC command names, the `end-file` `reason` values,
and the per-file `pause` load option are the contract this design rests on. Record
a minimum-supported mpv version, verify it in `doctor`, and treat the hard
dependency (fixed rulings) as a *versioned* one — a too-old mpv is a
client-observable `PLAYER_UNAVAILABLE`-class fault, not a silent behavioural
drift. The exact minimum is an implementation-mission detail; the requirement to
pin one is settled here.

---

## 2. The IPC protocol layer

### Transport and framing

A unix domain socket at `--input-ipc-server`, connected with
`asyncio.open_unix_connection`. Commands are newline-delimited JSON; responses
and events are newline-delimited JSON.

### Command set

| Command | JSON | Purpose |
|---------|------|---------|
| loadfile | `{"command": ["loadfile", <path>, "replace"], "request_id": N}` | Play a part (§3), issued **only by the loop**. A per-file `{"pause": "yes"}` option loads it paused for prev/next-while-paused and for a post-crash reload while paused (Fork B). The response confirms the file was *queued*, not that it is *playable* — a bad or corrupt file is accepted here and surfaces later as an `end-file` reason `error`. |
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
`request_id` arrives.

**On socket EOF the reader fails *every* pending command future**, not only the
loop's ended-future. Without this, an in-flight `pause`/`resume`/`quit` sent just
before the crash would hang for its full timeout before surfacing — dead time for
no reason. The reader drains the pending-future map, resolving each with a
crash/connection-lost error, then signals the supervisor. This and resolving the
ended-future with reason `crashed` (§1) are the two halves of one guarantee: a
crash leaves no await orphaned (the I7 loop-liveness invariant, §6).

`MpvClient` serialises its own writes (one send lock or an internal send queue),
because three callers write concurrently: the loop (`loadfile`), the suspension
(`pause`/`resume`), and shutdown (`quit`). This runs on **one asyncio event loop
in one thread** — the send lock is about *explicitness of interleaving at `await`
points*, not a defence against parallel OS threads. There are no data races here;
there are only interleavings, and the lock makes a framed command an
uninterruptible unit so two callers cannot splice half-written JSON onto the
wire.

### The async event stream

mpv pushes unsolicited events with no `request_id`. The reader classifies each
line: a `request_id` present → resolve a pending future; an `"event"` key
present → dispatch to the event handler; anything else → log and skip (a
malformed line never kills the reader).

The one event that drives the state machine is **`end-file`**, discriminated by
its `reason`:

| `reason` | Meaning | Loop action |
|----------|---------|-------------|
| `eof` | natural end of the loaded part | advance (post `Rotate`) — this is Z `AutoAdvance`, still guarded by the explicit `is_paused` check (§3) |
| `stop` / `redirect` / `quit` | deliberate teardown or replace | do NOT advance |
| `error` | bad/corrupt file | record a per-part fault (F3), advance past it |
| `crashed` (synthetic) | process died; **not** an mpv event | do NOT advance; wait for `ready`, re-play the current part (§1) |

The first four are mpv's own `end-file` reasons. `crashed` is **not** an mpv
event — there is no `end-file` on a crash. It is a synthetic reason the reader
injects into the loop's ended-future on socket EOF (§1, §2), so the one channel
the loop awaits carries every way a part can end, including a crash. `EndFileReason`
is the enum; the reader is the only producer of the synthetic member.

The four real reasons are the exact analog of today's `TrackEnd`: `eof` ↔ clean
exit code 0, `error` ↔ non-zero exit, `stop`/`redirect` ↔ user-interrupt.
`TrackEnd` is re-expressed over an `EndFileReason` enum instead of a process exit
code.

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
It becomes: **wait for mpv `ready`**, then `loadfile target` (paused per the
suspension flag — see prev/next), then await the load's **ended-future**, which
the reader resolves when an `end-file` event arrives for the current load — or,
on a crash, with the synthetic `crashed` reason. `InterruptRace` is reused almost
verbatim: it still races an ended-future against the `interrupt` Event; only the
future's source changes (an mpv event, not `proc.wait()`). `_finish` reads the
`EndFileReason` instead of an exit code.

**The `WaitReady` step is a real wait, not a busy-retry.** Deleting the
suspension gate (below) removes the loop's only blocking point; the mpv-`ready`
gate replaces it. Before any `loadfile` — at startup, and after each crash — the
loop *awaits* mpv reaching `ready` (an event the supervisor sets on connect),
rather than issuing `loadfile` into a not-ready client and getting refused into a
fault, then spinning to retry. A refuse-into-fault busy-retry would be a
throughput sink and would clutter status with transient faults for the normal
startup and post-crash windows. `WaitReady` is a single explicit `await`; the
loop parks there exactly as long as mpv takes to come up, and no longer.

**On the `crashed` reason** the loop does not advance the cursor. It returns to
`WaitReady`, then replays the **current** part — honouring `is_paused` (reload
with `pause=yes` when paused) — as §1 describes. This is the loop half of
crash recovery; the supervisor half is process restart only.

`_wait_for_playable` (pool empty in `generating-first`) is unchanged: mpv sits
idle with no file; the loop blocks on `channel.changed` until a part is
playable.

### Per-operation mapping

| Operation | Today | With mpv |
|-----------|-------|----------|
| **play / switch** (`PlayAlbum`, `SwitchProgram`, `SwitchSelection`) | control signal moves source, interrupts loop, loop spawns part 1 | same signal + interrupt; loop `loadfile` part 1 (playing); suspension reset drops any pause. **T1** holds — one mpv, one loaded file. |
| **auto-advance** (`AutoAdvance` → `Rotate`) | `proc.wait()` returns 0, loop posts `Rotate`, spawns next | `end-file` reason `eof`, loop checks `is_paused`, then posts `Rotate`, `loadfile` next. **T3** holds *with* the explicit `is_paused` guard (§3, kept from loop.py:159): an `eof` mpv buffered just before the user paused must not advance-then-load under "paused." |
| **pause** (`Pause`) | tear player down (SIGTERM), record wall-clock `ResumePoint` | `set_property pause true`; set paused flag; emit change. mpv freezes gapless. Loop is unaffected — still awaiting an `eof` that will not come. |
| **resume** (`Resume`) | re-spawn player seeked to the frozen offset | `set_property pause false`; clear paused flag; emit change. mpv continues from the exact decoder position — click-free, no reload, no seek. |
| **prev / next** (`Prev`, `Next`) | move cursor, interrupt loop, loop re-spawns seeked-or-fresh | move cursor, interrupt loop; loop `loadfile` the new part, **with `pause=yes` when the suspension flag is set** (Fork B — stays paused, new part from offset 0). |
| **stop / off** (`Stop`, `TurnOff`) | control signal → idle, loop kills player | control signal → idle; loop sends `stop`; mpv returns to idle. Suspension reset. **T1/T2** idle shape. |

### Why T1–T7 still hold

The transport invariants are properties of the *source* state, which mpv does
not touch:

- **T1** (single active source): one mpv, at most one loaded file.
- **T2** (now-playing iff active): the cursor lives in `Program`/`SelectionPlayback`, unchanged.
- **T3** (paused is suspended): *cheaper* under mpv but **not fully intrinsic**,
  and the explicit `is_paused` guard at the advance decision stays (as loop.py:159
  does today). A paused mpv reaches no *new* `eof`, so the steady state costs no
  gate-parking — but mpv can buffer and emit an `eof` for the current part in the
  instant just before the user pauses; that in-flight `eof` must not
  advance-then-load while the mode reads `paused`. The guard, not the absence of
  events, is what makes T3 hold. What mpv *does* let us delete is the **wall-clock
  gate mechanism** — the `wait_resumed` park that used to hold the loop for the
  whole pause. We delete that gate; we keep the one-line T3 check.
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

The loop reads `suspension.is_paused` at two points: at the **advance decision**,
as the T3 guard against an in-flight `eof` advancing under "paused" (§3); and at
**`loadfile` time**, to decide whether a reload loads paused (Fork B) — for a
prev/next while paused, and for a post-crash reload while paused (reload with
`pause=yes` so a crash while paused does not silently resume playing). The
wall-clock gate that parked the loop for the whole pause is gone — mpv's internal
pause holds the audio; the loop no longer blocks on a resume gate. What replaces
that gate as the loop's blocking point is the mpv-`ready` gate (`WaitReady`),
not a pause gate.

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

### Concurrency: pause/resume stay direct (settled)

`service.pause()`/`resume()` are today *direct* calls, not routed through the
`ControlChannel`, and they **stay direct** (peer-reviewed, gvr + kpz — §8). With
mpv they become IPC calls that can interleave with an `eof`-driven `Rotate`. The
outcome is benign — pause lands on part N or, if the `eof` was processed first,
on the freshly-loaded N+1 — and pause/resume mutate **no source state** (they
flip mpv's internal pause and the one `is_paused` flag; they do not move the
cursor), so there is nothing for the single-writer to serialise. Routing them
through the channel would be model-literal but would make pause touch the player
from the writer path as well as the loop, for no invariant gained.

This is safe because the daemon runs on **one asyncio event loop in one thread**.
"Race" here means an interleaving at `await` points, not two OS threads touching
shared memory — there are no data races to guard. `MpvClient`'s send lock keeps
each framed command an uninterruptible unit on the wire (§2); it is explicitness
about interleaving, not a thread mutex.

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
| mpv missing/too-old at startup | spawn or connect fails | routed through the same restart-with-cap path as a crash (`SpawnFail`/`ConnectFail` → `crashed` → retry); after the cap, the terminal `failed` state *is* `PLAYER_UNAVAILABLE`. Daemon stays up (notifications unaffected); every program command reflects the fault |
| mpv missing at first program | (does not occur — eager spawn) collapses to the startup case | terminal `failed` = `PLAYER_UNAVAILABLE` |
| mpv crash mid-playback | socket EOF / process exit | `PLAYER_CRASH`; reader resolves the loop's ended-future with `crashed` and fails every pending future (§2); supervisor restarts the process; loop replays the current part from start honouring `is_paused` (§1); `PLAYER_FAILED` if the restart cap is hit |
| IPC write failure (`BrokenPipeError` on send) | send raises | treated as a crash signal → the same restart path (reader will also see EOF) |
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

```text
MpvState ::= down | starting | ready | crashed | restarting | failed
```

- `down` — no process (before spawn, after shutdown).
- `starting` — process spawned, socket not yet connected.
- `ready` — process alive, socket connected, commands accepted.
- `crashed` — process/socket died; on entry the reader resolves every pending
  command future and the loop's ended-future (reason `crashed`), then a restart is
  owed.
- `restarting` — respawning within the cap.
- `failed` — restart cap exceeded; given up; standing hard fault.

### State schema (sketch)

```text
MpvLifecycle
  state       : MpvState
  processes   : 0 .. 1          -- live mpv processes
  readers     : 0 .. 1          -- live reader tasks
  restarts    : 0 .. maxRestarts
  fault       : ZBOOL           -- a standing client-observable fault
  pendingCmds : 0 .. maxInFlight  -- unresolved command futures (request_id map)
  loopAwait   : ZBOOL           -- the loop is awaiting an ended-future
  loadfileBy  : ℙ ACTOR         -- actors that have issued loadfile
  ----------------------------------------------------------------
  (state = ready  ⟺ processes = 1 ∧ readers = 1)   -- I1a: ready ⟺ connected
  (state ∈ {down, failed} ⟹ processes = 0 ∧ readers = 0)
  processes ≤ 1                                     -- I2: never double-spawn
  readers   ≤ 1                                     -- I5: one reader
  (fault = ztrue ⟺ state ∈ {crashed, restarting, failed})  -- I3
  restarts ≤ maxRestarts                            -- I4
  (state = failed ⟹ restarts = maxRestarts)
  (state ∈ {crashed, down, failed} ⟹ pendingCmds = 0)  -- I7: no orphaned futures
  loadfileBy ⊆ {loop}                              -- single-loadfile-ownership
```

### Invariants to prove

These are the invariants **jms will formalize** in `docs/mpv-program-player.tex`.
The first six were in the earlier draft; I7 and single-loadfile-ownership are
kpz's additions, and I1 and I6 are tightened per the peer review.

- **I1 — the loop waits for ready, it does not send into not-ready.** A
  `loadfile` is issued only in `ready`, and the loop reaches that point by
  *awaiting* the `ready` transition (`WaitReady`), never by issuing into a
  not-ready client and being refused into a fault, then busy-retrying. The model
  proves no `loadfile` is enabled outside `ready` **and** that the loop's path to
  a `loadfile` is a wait on `ready`, not a refuse-and-retry cycle. (`pause`/`stop`
  likewise require `ready`.)
- **I2 — at most one process.** No restart race spawns a second mpv.
- **I3 — fault ⟺ a fault mode (strict).** A client sees a standing fault in
  exactly the three fault states (`crashed`, `restarting`, `failed`); `down` and
  `starting` are clean, so normal bring-up and post-shutdown never report a fault.
- **I4 — the restart cap terminates.** `restarts` is monotone and bounded;
  reaching the cap ⟹ `failed` with no further spawn (no hot loop).
- **I5 — one reader.** The reader is alive iff `ready`; started on connect,
  cancelled on crash/shutdown.
- **I6 — the process lifecycle does not corrupt source state, and recovery
  honours pause.** A crash/restart leaves the daemon cursor untouched; the reload
  targets the same current part from offset 0; and the reload **honours the
  paused flag** — reloaded with `pause=yes` when `is_paused`, so recovery never
  silently resumes playing. Recovery reaches `loadfile` through the explicit
  `WaitReady` transition, never a busy-retry.
- **I7 — loop-liveness-across-crash (no orphaned await).** A crash resolves
  *every* pending command future **and** the loop's in-flight ended-future (the
  latter with the synthetic `crashed` reason). No `await` in the loop or in any
  command coroutine is left hanging by a crash; `pendingCmds` returns to 0 and
  `loopAwait` is resolved on the `CrashDetected` transition. This is the
  invariant that the earlier draft's supervisor-reloads split violated — a crash
  left the loop awaiting an ended-future no `end-file` would ever resolve.
- **single-loadfile-ownership.** Only the loop issues `loadfile`
  (`loadfileBy ⊆ {loop}`). The supervisor spawns, connects, detects crashes, and
  restarts the *process*; it never loads a file. This forecloses the double-load
  the supervisor-reloads split allowed (supervisor reload racing the loop's next
  `loadfile`).

### Transitions to model

`Spawn` (down→starting), `Connect` (starting→ready), `WaitReady` (the loop's
await-then-`loadfile` step, enabled on entry to `ready`), `SendWhenReady`,
`CrashDetected` (ready→crashed — resolves every pending future and the loop's
ended-future with `crashed`, per I7), `Restart` (crashed→starting if
`restarts < maxRestarts` else crashed→failed), `ReplayCurrent` (the loop's
post-crash reload of the current part, honouring `is_paused`, per I6),
`Shutdown` (any→down via quit).

### What ProB should exhaust

No reachable state issues a `loadfile`/`pause`/`stop` while not `ready`, and the
loop reaches `loadfile` by waiting on `ready` rather than refuse-and-retry (I1);
the restart cap terminates (no infinite respawn, I4); `crashed` always leads to
`ready` again or `failed` (no wedge); no crash leaves a pending future or the
loop's ended-future unresolved (I7); only the loop issues `loadfile`
(single-loadfile-ownership); at most one process and one reader throughout (I2,
I5); a post-crash reload honours the paused flag (I6).

The `.tex` is a design-mission deliverable, authored by jms; implementation does
not dispatch until it is `fuzz`-clean, ProB-model-checked, and its findings are
resolved.

---

## 7. Decomposition / write-set

### Add

| Module | Responsibility |
|--------|---------------|
| `programs/mpv/mpv_client.py` | `MpvClient` — one live connection: process handle, socket, reader task, request/response correlation, self-serialised sends. `send(command) -> response`, an event subscription, `is_ready`. On socket EOF the reader **fails every pending command future** and **resolves the loop's ended-future with `crashed`** (I7). |
| `programs/mpv/mpv_supervisor.py` | `MpvSupervisor` — spawn / connect / crash-detect / restart-with-backoff-and-cap across connections; owns the standing mpv fault surface and the `ready` signal the loop awaits. **Never issues `loadfile`** (single-loadfile-ownership). |
| `programs/mpv/mpv_program_player.py` | `MpvProgramPlayer` — the loop-facing player: `play(part) -> handle` (`loadfile` + an ended-future resolved by `end-file` or `crashed`), `stop()`, `pause()`, `resume()`, and an `await_ready()` the loop uses for `WaitReady`. |
| `programs/mpv/orphan_reaper.py` | `OrphanReaper` — cross-restart startup hygiene enforcing I2. Before the first spawn, `reap()` quits an mpv left on our IPC socket by an unclean prior exit — by *socket identity* (connect + `quit`, then unlink the stale path), never by a recorded pid. No kill-by-pid fallback (§1). |
| `types_programs/mpv_event.py` | `MpvEvent`, `EndFileReason` enum (`eof`/`stop`/`redirect`/`quit`/`error` plus the synthetic `crashed` the reader injects), `MpvCommand`/`MpvResponse` value types (PY-IC-9: types in their own module). |

The design mission decides the final split (one `mpv/` package vs. flatter),
per the "design mission's output IS the write-set" rule. The above is the shape,
not a mandate.

### Delete

- `programs/subprocess_player.py` — the `ffplay`-per-part `SubprocessPlayer` /
  `SubprocessHandle`.
- `programs/resume_point.py` — `LiveTrack`, `ResumePoint`. mpv owns position.

### Modify

- `programs/loop.py` — `_play`: `WaitReady` (await mpv `ready`) then `loadfile`
  (paused per flag) + await ended-future; `_finish`: read `EndFileReason` not an
  exit code, keep the explicit `is_paused` advance guard (loop.py:159), and on the
  `crashed` reason replay the current part (via `WaitReady`, honouring
  `is_paused`, cursor unmoved) rather than advancing; drop the offset/seek path.
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
- `doctor` — check mpv is present **and at or above the pinned minimum version**;
  a missing or too-old mpv is a client-observable `PLAYER_UNAVAILABLE`-class
  finding, not a silent pass (§1 version pin).

### Testing seam

Unit tests cannot spawn real mpv in CI. The injection seam is the
`Player`/`MpvClient` boundary (as `SubprocessPlayer` is injected today): a
`FakeMpvClient` records commands and lets a test inject `end-file` events and
crashes. Per-event dispatch, the malformed-line path, the timeout path, and the
crash/restart path are all tested against the fake. The crash tests assert the
modeled invariants by name: I7 (a simulated EOF resolves every pending command
future and the loop's ended-future — no test hangs), single-loadfile-ownership
(the fake sees `loadfile` only from the loop, never the supervisor), and I6's
paused-honoured-on-reload (a crash while paused replays with `pause=yes`).

---

## 8. Resolved decisions

The four questions the earlier draft raised are **settled** — peer-reviewed
consensus, gvr + kpz agree. They are recorded here as rulings, not open items.

1. **mpv crash recovery: reload the current part from offset 0.** Not exact
   position. Recover-exact requires continuous `time-pos` polling and reintroduces
   the wall-clock machinery this design deletes; a crash is rare, and replaying
   the current part from the top is acceptable and observable. The reload honours
   `is_paused` (§1). **Resolved: reload from offset 0.**

2. **pause/resume stay direct.** They are not routed through the `ControlChannel`
   single-writer. They **mutate no source state** — they flip mpv's internal pause
   and the one `is_paused` flag, never the cursor — so there is nothing for the
   writer to serialise; and on one asyncio thread the pause-vs-`eof` interleaving
   is benign (§3). Routing them through the channel buys no invariant and makes
   pause touch the player from two paths. **Resolved: direct IPC calls.**

3. **`--volume=30` static duck.** The reduced music volume is a constant for this
   change (matching today's `_MUSIC_VOLUME = 30`). Dynamic ducking — lowering mpv
   volume while speech plays, raising it after — is trivial under mpv and a
   worthwhile **follow-up**, not part of this design (§5). **Resolved: static
   `--volume=30` now; dynamic ducking later.**

4. **Eager spawn with a client-observable fault; never fail the daemon over mpv.**
   mpv is spawned at bring-up; a spawn failure records a standing
   `PLAYER_UNAVAILABLE` fault and surfaces it, but the daemon — and the independent
   notification tier — stays up. Failing the whole daemon on missing mpv would take
   down notifications, which do not depend on mpv. **Resolved: keep the daemon up,
   surface the fault.**

### Framing notes (settled, recorded for jms and the implementer)

- **Single-thread asyncio.** The daemon runs on one event loop in one thread.
  "Races" are interleavings at `await` points, not parallel-thread data races; the
  `MpvClient` send lock is explicitness about interleaving (a framed command is an
  uninterruptible unit on the wire), not a thread mutex (§2, §3).
- **`loadfile` confirms queued, not playable.** The `loadfile` response means mpv
  accepted the file into its playlist, not that it decodes. A bad or corrupt file
  surfaces later as an `end-file` reason `error` (F3), never at the `loadfile`
  call (§2).
- **Pin the mpv version.** The IPC command names, the `end-file` `reason` values,
  and the per-file `pause` option are the contract; record and verify a
  minimum-supported mpv version in `doctor` (§1).
