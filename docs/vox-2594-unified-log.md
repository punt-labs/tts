# vox-2594: One vox.log, every process a direct writer

## The claim to fix

v4.12.5 promised "one vox.log". It ships two. `vox.log` holds the daemon's
records plus records from clients that opened a WebSocket for real work
(synthesis, chime). Every hook that does **no** daemon work — the
`Stop` / `UserPromptSubmit` / `SubagentStop` skip paths and config updates —
opens no WebSocket, so its records drain through the `atexit` fallback into
`vox-fallback.log`. Those hooks are the majority of fires, so the "fallback"
is the primary path and the largest file (4.2 MB + rotations vs. 404 KB).

The transport is the bug. `LogShipper` (`log_ship.py`) buffers each record and
drains it over the socket the client opens *for its actual RPC*. A
connectionless client never opens that socket, so its records only ever reach
the fallback. No amount of tuning the fallback fixes a design that cannot carry
the largest client class on its primary path.

## Decision: candidate B — every process appends directly to vox.log

Every process — daemon included — appends its own records to a single
`vox.log` through the multi-writer-safe `O_APPEND` line writer already in the
tree (`AtomicAppendLog`), with rotation guarded by a `flock` shared/exclusive
protocol in the DES-013 size-check-then-rename shape. There is no daemon
dependency for logging, so **there is no fallback file and no ship transport at
all**.

This is not new machinery. `AtomicAppendLog` already gives atomic single-line
`O_APPEND` writes, 0600 via `PrivateState`, `O_NOFOLLOW`, and a rename-chain
rotation. It is used today only for the fallback. The fix points *every*
writer at `vox.log` through it and deletes the ship/buffer/wire/sink apparatus
that existed only to route client records to the daemon.

### Why the alternatives lose

**A — no-daemon-work hooks open a brief connection to ship their line.** Every
fast hook pays a daemon round-trip on its logging hot path, the exact cost
DES-017 exists to avoid, and it *still* needs a fallback for the daemon-down
case — so the two-file split survives. Rejected: it violates invariant (2) and
does not even retire the fallback.

**C — clients append to a local spool the daemon drains into vox.log.** Keeps
the daemon as sole writer at the price of a drain loop, a spool file lifecycle,
and latency between an event and its visibility in `vox.log` (a hook's line is
invisible until the daemon next drains). The daemon-sole-writer property C buys
is exactly what `AtomicAppendLog`'s `O_APPEND` atomicity already provides
without a second process in the path. Rejected: more code, more moving parts,
worse latency, for a guarantee B gets for free.

**B** deletes code, removes a whole IPC path, and reuses a proven sink. Its one
cost is that `vox.log` is now written concurrently by many processes at high
volume, so the rotation race that was negligible for the low-volume
`vibe-trace.log` becomes real and must be closed by a lock. That is the whole
of the new work.

## Invariants the design satisfies

Stated verbatim from the mission contract:

1. **One `vox.log` receives daemon records AND every client record, including
   hooks that do no daemon work (Stop/UserPromptSubmit/SubagentStop skip paths,
   config updates).** Every process installs the same append handler pointed at
   `vox.log`; a record reaches the file by a local `O_APPEND` write with no
   socket, so a connectionless hook lands its lines exactly like the daemon
   does.
2. **No hook pays a daemon round-trip on its logging hot path (DES-017).** The
   hot path is `flock(LOCK_SH)` → `open(O_APPEND)` → `write` → `close` →
   `flock(LOCK_UN)`: local syscalls, no WebSocket, no daemon. A hook blocks only
   during an in-progress rename chain (rare, sub-millisecond), never on a
   network round-trip.
3. **Rotation is safe under concurrent writers — no lost or torn lines, no write
   to a renamed file.** The `flock` protocol below makes a rename impossible
   while any writer holds an open append fd, and makes at most one rotator run
   the rename chain. Modeled formally in `vox-2594-log-rotation.tex`.
4. **Log files keep 0600.** `AtomicAppendLog` opens through `PrivateState`
   (`O_CREAT|0600`, `fchmod` re-tighten, `O_NOFOLLOW`); the rotation step renames
   with `Path.replace`, preserving each file's 0600 mode, and a fresh `vox.log`
   is recreated 0600 on the next open.
5. **`vox-fallback.log` is DELETED outright — no fallback file, no
   migration/compat/shim/bridge of any kind.** The ship transport is the only
   thing that produced a fallback; deleting the transport deletes the fallback.
   No code reads or writes the old file after this change.
6. **Observability stays persistent-file, never stderr-only (DES-046).** Both
   configs install exactly one handler — the file-append handler. Neither
   installs a `StreamHandler`. The daemon's `StandardErrorPath` stays gone.

## Mechanism

### The one sink, the one handler

`AtomicAppendLog(vox.log)` is the single sink. A new `logging.Handler` subclass
(`AppendLogHandler`) renders each record to one line with the shared
`LOG_FORMAT` and hands the line to that sink. `emit` never raises and never
re-enters `logging`: a formatting fault goes to `handleError`, and the sink's
own I/O errors go to `sys.__stderr__` (as `AtomicAppendLog` already does), so a
logging failure can never crash the hook, tool, or daemon that logged.

Both entry points install this one handler:

- **Daemon** (`configure_daemon_logging`): root handler is `AppendLogHandler`
  over `vox.log`. The former `PrivateRotatingFileHandler` file handler is
  removed — one writer path for everyone, no second rotation mechanism racing
  the append sink.
- **Client** (`configure_client_logging`): root handler is the same
  `AppendLogHandler` over the same `vox.log`, stamped with the process `role`
  so a client line reads `client.<role>.<module>: …` and greps apart from a
  daemon line. No ship handler, no fallback tree.

The record is escaped exactly once: the handler renders `LOG_FORMAT` plain and
`AtomicAppendLog.append` applies the shared `SANITIZER` to the whole line, so a
newline or control byte in any field (a client-shipped name, a provider error
body) stays one physical line. This preserves the log-injection defense
(proposal §2e) without double-escaping.

### The rotation lock protocol (the only new safety code)

The append sink today rotates best-effort with no lock — correct for
`vibe-trace.log` (62 KB over the project's life), unsafe for a high-volume
multi-writer `vox.log`. Two races open:

- **Double rotate:** two writers both observe oversize and both run the rename
  chain, misfiling or dropping a backup slot.
- **Write to a renamed file:** a writer opens an fd on `vox.log`, a second
  writer rotates `vox.log → .1` underneath it, and the first writer's line lands
  in the renamed backup instead of the active log.

Close both with one advisory lock on a **stable** file `vox.log.rotate.lock`
(never itself renamed, so the lock identity survives rotation), using the
DES-013 pattern from `playback.py`:

- **Append (hot path, every writer):** take `LOCK_SH`, then
  `open(O_APPEND) → write → close` on `vox.log`, then release. Shared locks do
  not block each other, so concurrent appends still run in parallel; the added
  cost is one uncontended `flock` pair per line.
- **Rotate (only when a writer observes `size + line > maxBytes`):** release
  `LOCK_SH`, take `LOCK_EX`, **re-check the size under the exclusive lock**
  (idempotency: a rotator that queued behind another finds the fresh small file
  and skips), run the rename chain if still oversize, release `LOCK_EX`, then
  re-take `LOCK_SH` and append the line.

`maxBytes` is a soft cap: when the re-check finds a peer already rotated
(`SkipRotate`), this writer still appends its line to the fresh file, so the
active log can sit up to one line over `maxBytes`. This is benign and
intentional — bounding the file to the byte would require holding the exclusive
lock across the append, serializing every writer, which the shared-lock design
exists to avoid.

Why it is safe:

- `LOCK_EX` cannot be acquired until every `LOCK_SH` holder has released, and a
  writer holds `LOCK_SH` across its entire `open→write→close`. So **no rename
  runs while any writer has an open append fd** — the "write to a renamed file"
  race is closed. (Formal invariant: `rotating ⇒ no writer is appending`.)
- `LOCK_EX` is exclusive, so **at most one rotator runs the rename chain**, and
  the re-check under the lock makes a redundant rotate a no-op — the "double
  rotate" race is closed. (Formal invariant: `#rotating ≤ 1`.)
- Every line is written under `LOCK_SH` to the current active inode, which
  `O_CREAT` always recreates — **no lost line**.

Latency: the hot path adds two local `flock` syscalls (microseconds,
uncontended). A writer blocks only when a rotation is actively renaming —
bounded by one rename chain of `backupCount` renames, sub-millisecond, once per
~5 MB across *all* writers. This honors DES-017: `flock` on a local file is not
a daemon round-trip.

**Flagged tradeoff for leader/operator review:** the daemon logs synchronously
on its asyncio event loop today (the current `RotatingFileHandler` already
blocks there). Under B the daemon's `emit` adds one uncontended `flock` per
record plus, rarely, a sub-millisecond wait behind a peer's rename. This is the
same order as today's in-loop rotation and needs no thread offload now (YAGNI).
Named here so the round-trip/rotation tradeoff is an explicit decision, not a
surprise found in implementation.

### Failure behavior when the log dir is missing or unwritable

`AtomicAppendLog.append` already `ensure_private_tree()`s best-effort and, on
any `OSError` (missing dir it cannot create, unwritable path, `ENOSPC`), writes
a one-line note to `sys.__stderr__` and returns — never raising, never looping a
short write (which another writer's line could split). The append handler
inherits this: a logging failure degrades to a stderr note, never a crashed
hook. `is_writable()` reports health of the *path* (not one writer's last
write) so `mic:status` / `vox doctor` can surface an unwritable log.

## Implementation write set

### Create

- `src/punt_vox/log_append_handler.py` — `AppendLogHandler(logging.Handler)`:
  renders `LOG_FORMAT`, stamps the client role into the logger name, hands the
  line to a shared `AtomicAppendLog`. `emit` never raises, never re-enters
  `logging`. This one class replaces both `DaemonLogHandler` (ship) and the
  daemon's `PrivateRotatingFileHandler`.
- `src/punt_vox/log_format.py` — the three surviving constants `LOG_FORMAT`,
  `LOG_DATE_FORMAT`, and the `Role` literal, extracted from `log_wire.py` before
  that module is deleted. (Alternatively fold into `logging_config.py`; a small
  dedicated module keeps them importable without the config stack.)

### Modify

- `src/punt_vox/append_log.py` — add the `flock` shared/exclusive rotation
  protocol: a stable `<path>.rotate.lock` fd, `LOCK_SH` held across
  `open→write→close` on every `append`, and a `LOCK_EX`-guarded rotate with a
  size re-check. This is the core safety change.
- `src/punt_vox/logging_config.py` — both `configure_daemon_logging` and
  `configure_client_logging` install `AppendLogHandler` over `vox.log`. Delete
  `_FALLBACK_FILE`, `_SHIP_HANDLER_FACTORY`, `_FILE_HANDLER_FACTORY`, the
  `PrivateRotatingFileHandler` wiring, and `reapply_client_log_level`'s handler
  walk stays valid (it sets levels on whatever handlers exist).
- `src/punt_vox/client.py` — delete `_flush_logs`, the `LogShipper.active()`
  call, and the `log_ship` import; `connect`/`close` no longer flush logs.
- `src/punt_vox/server.py` — delete `PeriodicFlusher` import, `_log_flusher`,
  and its `start`/`stop`/`atexit` wiring.
- `src/punt_vox/voxd/daemon.py` — remove the `"log": LogHandler()` route;
  clients no longer ship log frames.

### Delete

- `src/punt_vox/log_ship.py` — `LogShipper`, `DaemonLogHandler`, `WsSender`.
- `src/punt_vox/log_flush.py` — `PeriodicFlusher`.
- `src/punt_vox/voxd/log_sink.py` — `LogHandler` (the daemon-side frame
  receiver).
- `src/punt_vox/log_wire.py` — `LogRecordWire` and the whole `{"type":"log"}`
  wire schema; surviving constants moved to `log_format.py` first.
- The runtime file `~/.punt-labs/vox/logs/vox-fallback.log` (+ rotations) — see
  cleanup below. No code writes it after this change.

**Tests** — delete `tests/test_log_ship.py`, `tests/test_log_flush.py`,
`tests/voxd/test_log_sink.py`, `tests/test_log_wire.py` (the wire-schema tests);
add `tests/test_log_append_handler.py`; extend `tests/test_append_log.py` with
the concurrency/rotation cases below.

The specialist owns the final shape — split or rename as the code wants; this
set is the forward-integration target, not a prescription to preserve any file.

## Test plan (each case asserts an invariant by name)

- **"skip-hook line lands in vox.log"** — drive a hook handler on a skip path
  (a `Stop` with nothing to speak, a `UserPromptSubmit`, a config update) with
  no daemon reachable; assert the emitted line is in `vox.log` and that
  `vox-fallback.log` was never created. (Invariant 1.)
- **"logging opens no socket"** — assert that emitting a record constructs no
  `VoxClient`/WebSocket; the append handler touches only the filesystem.
  (Invariant 2 / DES-017.)
- **"rotation under two concurrent writers loses no lines"** — spawn N processes
  each appending M uniquely numbered lines, sized to force ≥1 rotation; assert
  the union of `vox.log` + all backups contains exactly N×M lines, each intact
  (no torn line, no interleave), none in a nonexistent slot. (Invariant 3.)
- **"no write to a renamed file"** — a rotation cannot proceed while a writer
  holds `LOCK_SH`; assert (via a controlled two-writer harness holding the
  shared lock) that `LOCK_EX` blocks until the shared holder releases, so no
  append targets the renamed inode. (Invariant 3.)
- **"log files keep 0600"** — `stat` `vox.log` and every backup after a forced
  rotation; each is `0o600`. (Invariant 4.)
- **"fallback path no longer exists"** — `import punt_vox.log_ship` /
  `log_flush` / `voxd.log_sink` / `log_wire` each raise `ModuleNotFoundError`;
  a tree grep for `vox-fallback` returns nothing in `src/`. (Invariant 5.)
- **"observability stays persistent-file"** — after either configure call, no
  root handler is a `StreamHandler`; the sole handler is `AppendLogHandler`.
  (Invariant 6.)
- **"log dir unwritable degrades, never crashes"** — point the sink at an
  unwritable dir; `emit` returns, writes a `sys.__stderr__` note, raises
  nothing.

## Live-verify plan (running machine, part of done)

1. `make install`, then `vox daemon restart` (the daemon loads code at startup;
   a stale daemon serves the old sink).
2. Drive a real session: a turn with **no** speech (fires a `Stop` skip hook),
   a `UserPromptSubmit`, and a `/vox log debug` config update. `grep` `vox.log`
   — each hook's line is present. (Invariant 1 on the live machine.)
3. Confirm `vox-fallback.log` **stops growing** and, after the cleanup below,
   does not reappear. (Invariant 5.)
4. Speak something (`mic:unmute`) and confirm the daemon's synthesis line and
   the hook's client line share the one `vox.log`, interleaved and greppable.
5. Force a rotation (temporarily lower `maxBytes`, or run a burst of concurrent
   hook fires across sessions); confirm `vox.log.1..N` appear at `0600` with no
   torn lines and no lost count. (Invariants 3, 4.)

## Cleanup of stale runtime files

`tts.log*`, `voxd.log*`, `hook-errors.log`, and `voxd-stderr.log` are pre-fdmm
leftovers. A grep of `src/`, `hooks/`, and `scripts/` shows **no current code
references any of them** (the only hit is `daemon.py`'s unrelated `LogHandler`
import). `vox-fallback.log` is the one stale file current code still writes, and
this change removes that writer.

One-time operational cleanup during live-verify (not a code change — these are
user-dir runtime files, and Punt Labs keeps no migration code):

1. `mv ~/.punt-labs/vox/logs/{tts.log*,voxd.log*,hook-errors.log,voxd-stderr.log,vox-fallback.log*} .tmp/`
2. Run a full session (steps 2–5 above).
3. Confirm none of the moved files reappear and `vox.log` carries every record.
4. Delete the moved files.

## Formal model

Concurrent-writer rotation has invariants that silently corrupt on a wrong
transition (a rename while a writer holds an open fd → a misfiled line; a
double rotate → a dropped backup) — the formal-modeling trigger fires. The Z
specification is `docs/vox-2594-log-rotation.tex`: it models the writer/rotator
lock states with the shared/exclusive-exclusion and single-rotator invariants in
the state schema predicate, one operation schema per transition, and
`fuzz -t` clean. See that file for the state space and transitions the
implementation's rotation tests assert by name.
