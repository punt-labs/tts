# Logging Consolidation — Design & Write-Set (vox-fdmm)

Turns the P1#3 / P2 / P3 items of `docs/logging-proposal.md` into a concrete
architecture and an admission-ready write-set. Conforms to the revised
`punt-kit/standards/logging.md` (post-#225).

Scope: the **structural + observability** unit. The P1 security items
(0600 files, auth-rejection WARNING, music-prompt leak, daemon stderr
double-write, injection escaping) already shipped in vox-q637 (PR #337) and are
verified present in the code read for this design — this unit does not redo them,
it builds on them.

**No migration / compat / shim code anywhere.** Every superseded path is deleted
outright in the same change (forward integration, PY-RF-6): `tts.log`'s
multi-writer handler, both duplicated `dictConfig` blocks, the `hook-errors.log`
shell redirect. No legacy-format detector, no `*-migrate`, no fallback-to-old.

---

## 1. The problem, restated from the code

Verified by reading the tree, not the audit:

- **Five client process types each own a `PrivateRotatingFileHandler` on
  `tts.log`.** `logging_config.configure_logging()` (one `dictConfig`) is called
  by `server.py:881`, `hooks.py:69`, `cli_io.py:63`, `playback.py:108` — every
  one installs a rotating handler on the *same* file. `RotatingFileHandler` is
  documented multiprocess-unsafe on rollover; the file sits pinned at 5 MB, so
  the race fires routinely. The handler itself is safe *per process* (0600,
  atomic create) — the defect is **N processes, one file**.
- **The daemon owns a second `dictConfig`** in `voxd/config.py:246`
  (`DaemonConfig.configure_logging`), writing `voxd.log` (single writer, safe).
  Two configs drift; two files split-brain an operator.
- **Zero loggers on the Program state machine** (`grep getLogger`
  `voxd/programs/` state/program/service/loop = nothing). The
  `off→generating→playing→rotating` machine that `docs/audio-programs.tex`
  models is invisible in the log.
- **Asymmetric outcomes**: `FillRecorder.ready` (fill_recorder.py:52) records a
  generated track and logs nothing; only failures log.
- **Provider selection is unlogged** (`providers/__init__.py`), the AWS probe
  failure swallowed (`_has_aws_credentials` returns `False` silently).
- **Silent hook no-ops**: malformed stdin JSON (`hooks.py:111`) and
  config-absent (`hooks.py:439/464`) both return `{}`/`None` with no line.
- **7 INFO lines per chime**: router connect (`router.py:70`) → chime
  (`system_handlers.py:73`) → playback start (`playback.py:438`) → spawn-dump
  (`playback.py:250`) → ok (`playback.py:414`) → done (`playback.py:441`) →
  disconnect (`router.py:113`).
- **`mcp` framework request logger** unsuppressed; no vox-owned per-tool line.
- **Developer-format INFO**: `cache MISS: id= provider=openai …`,
  `Direct-play ok: provider=say voice= elapsed=0.512s`, `Session config:
  notify=c speak=n …`.

---

## 2. Consolidation architecture

### 2.1 One owner, one file

The daemon (`voxd`) becomes the **single writer** of the one durable log. Every
client process — MCP server, each hook, CLI, detached playback — ships its log
records to `voxd` over the **WebSocket it already opens** for its real work.
`voxd` re-emits each shipped record into its own file handler. `tts.log`
disappears; the split-brain disappears; the multi-writer race disappears because
there is exactly one writer.

```text
┌───────────── client process (mcp / hook / cli / detached) ─────────────┐
│  logging.getLogger(__name__).info("Stop hook: blocking…")              │
│        │                                                                │
│        ▼  (root handler)                                                │
│  DaemonLogHandler.emit ── format ──▶ LogShipper deque (bounded)         │
│                                          │        │                     │
│                                          │        └─ atexit / send-fail │
│                                          │              ▼               │
│         _VoxdTransport.connect() ── flush(ws) ─┐   AtomicAppendLog      │
│         _VoxdTransport.close()   ── flush(ws) ─┤   (vox-fallback.log,   │
│                                                 │    O_APPEND 0600)      │
└─────────────────────────────────────────────── │ ─────────────────────┘
                                                   │  {"type":"log",…} ×N
                                                   ▼  (fire-and-forget)
┌───────────────────────────── voxd daemon ──────────────────────────────┐
│  WebSocketRouter → LogHandler("log")                                    │
│        │  reconstruct LogRecord (role-tagged), never re-interpolate     │
│        ▼                                                                │
│  logging.getLogger("client.<role>.<name>").handle(record)               │
│        ▼                                                                │
│  PrivateRotatingFileHandler ─▶ vox.log   (single writer, 0600, 5MB×5)   │
└─────────────────────────────────────────────────────────────────────────┘
```

### 2.2 The wire message

A new send-only message type on the existing `/ws` socket. **No response** — the
daemon writes it and reads the next frame; the client never awaits an ack, so log
shipping adds no round-trip to the hot path.

```text
{
  "type":    "log",
  "role":    "hook" | "mcp" | "cli" | "playback",   # which client shipped it
  "name":    "punt_vox.hooks",                        # record.name (module)
  "level":   "INFO",                                  # record.levelname
  "created": 1721300000.123,                          # record.created (epoch s)
  "message": "Stop hook: blocking for voice summary"  # FINAL rendered text
}
```

Key decision: **`message` is the already-`%`-interpolated, already-SANITIZER-
escaped final string**, computed on the client. The daemon never re-interpolates
`args` — so an untrusted value that reached a client log call cannot forge a
second record inside `voxd.log` (Log Injection rule). The daemon emits `message`
verbatim as the record message with `args=None`. `level`/`created`/`name` are the
only structured fields, all machine-controlled or already-safe.

The daemon prefixes the logger name so `vox.log` distinguishes client from
daemon lines and names the origin process:

```text
2026-07-18 10:37:41 [INFO] client.hook.punt_vox.hooks: Stop hook: blocking for voice summary
2026-07-18 10:37:41 [INFO] punt_vox.voxd.programs.control_channel: music: generating_first → playing_filling
```

### 2.3 Ordering & backpressure

- **Buffer**: `collections.deque(maxlen=1024)` inside `LogShipper` (module
  singleton). `emit` is O(1) append; never connects, never blocks, never raises
  (a full deque drops the *oldest* record and bumps a drop counter). CPython
  `deque.append`/`popleft` are atomic, so a `VoxClientSync` worker-thread emit
  and the event-loop flush need no lock.
- **Flush points**: `_VoxdTransport.connect()` (right after handshake) and
  `close()`. Flushing on connect means buffered lines are sent **before** the
  real request frame, so the router handles them first (each `log` handler call
  is a fast file write) and the real RPC's `send_and_drain` never sees an
  unexpected frame. No mid-RPC flushing → no interleave with request/response
  framing.
- **Latency**: a short-lived client (hook, CLI, detached) opens a connection
  almost immediately, so its lines are durable within milliseconds. The
  long-lived MCP server flushes on each tool call; lines emitted between calls
  sit in the bounded deque until the next call or `atexit`. This is acceptable
  for diagnosis-not-monitoring (Open Decision **D2**).
- **Drop accounting**: if the deque ever drops, `LogShipper` writes a single
  `"log buffer overflowed: dropped N records"` line to the fallback file (not
  through logging — see recursion note below), so a lossy burst is itself
  traceable.

### 2.4 Daemon-down fallback (the 2-state selector)

When the client cannot ship — daemon unreachable at `connect()`, a `send`
raises, or the client never opens a connection at all (a config-absent hook that
returns before any voxd call) — the buffered records are written to a local
**`vox-fallback.log`** via `AtomicAppendLog`: `os.open(O_WRONLY|O_APPEND|O_CREAT,
0o600)`, `fchmod` re-tighten, one `os.write` per whole line, SANITIZER-escaped.
This is the exact `vibe_trace` model, generalized: multi-writer-safe by
construction (POSIX atomic `O_APPEND`), so many fallback-writing processes share
one file with no rotation race.

Fallback is triggered by:

- `LogShipper.flush(ws)` catching **any** exception from `ws.send` → route the
  in-flight batch to `AtomicAppendLog`.
- `connect()` raising `VoxdConnectionError` → the handler stays buffered; the
  `atexit` drain writes them to fallback.
- `atexit`-registered `LogShipper.drain_to_fallback()` → the tail of any process.

**Recursion guard**: `AtomicAppendLog`'s own error path must not call
`logging.*` (that would re-enter `DaemonLogHandler` → re-enqueue → re-fallback).
On `OSError` (disk full) it writes to `sys.__stderr__` best-effort and returns.
This differs from `vibe_trace.py`, which currently logs its `OSError` through
`logger.warning`; the shared `AtomicAppendLog` swallows-to-stderr instead, and
`vibe_trace` inherits that (safe — vibe-trace's warning previously went to
`tts.log`, which no longer exists).

### 2.5 Is this a state machine that needs a Z model? **No.**

The fallback is a **2-state, per-record, stateless selector**:
`daemon-reachable → ship` / `daemon-unreachable → append-local`. Justification
for skipping the Z ceremony, against the `CLAUDE.md` trigger:

- **No invariant spans a transition.** Each record is independently shipped or
  fallback-appended; there is no accumulated quantity (like the playlist's
  `pool ≤ 12`, "at most one fill active", "playing ∈ pool") that a wrong
  transition corrupts.
- **A wrong transition is self-correcting, not corrupting.** "Ship to a down
  daemon" simply fails the `send` and falls back. The worst outcome is a lost or
  duplicated *log line* — a diagnosability degradation, never state corruption,
  never a crash, never a UX-expensive-to-discover-late fault. That is the exact
  class the `bas7`/#291 model existed to prevent, and it does not apply here.
- **The trigger's own exclusions cover it.** `CLAUDE.md` excludes "pure I/O
  helpers … single-function bug fixes with no state." A sink selector is I/O
  plumbing.
- **The modeled machine is untouched.** The `off→generating→playing→rotating`
  Program machine already has its Z model in `docs/audio-programs.tex`. This
  design adds *observers* (log lines) to already-modeled transitions; an
  observer does not change the model's state space or invariants.

Recommendation carried to Open Decisions as **D6** (explicit "no Z model"
ruling) so the leader can ratify rather than infer.

---

## 3. The unified `dictConfig` owner

`logging_config.py` becomes the single logging owner, exposing two entry
configurations built from shared constants (format, date-format, max-bytes,
backup-count, the third-party + `mcp` suppression table):

- **`configure_daemon_logging()`** — root → `PrivateRotatingFileHandler` on
  `vox.log`, no stderr handler (q637). Called by `voxd` `main()`. Includes the
  0600 re-tighten warning sweep (moved verbatim from `DaemonConfig`).
- **`configure_client_logging(*, role, verbose=False)`** — root →
  `DaemonLogHandler` (ships over WS). No file handler on the client side at all.
  Called by every client entry point. `role` stamps the wire `role` field;
  `verbose` raises the ship handler to DEBUG for `--verbose` CLI.

Both call `PrivateState(log_file).ensure_private_tree()` **first**, which creates
and re-tightens `~/.punt-labs/vox` and `…/logs` to 0700 on every call — this is
the **djb re-tighten fix** (routed item 2): the client entry points that never
called `ensure_user_dirs()` now tighten a pre-existing loose dir on every
configure. Client tightening is best-effort (`PrivateState` logs a WARNING, never
crashes a hook); the daemon additionally fail-closes via `ensure_user_dirs()` in
`main()`, unchanged.

`DaemonConfig.configure_logging` / `_warn_on_loose_logs` / the logging constants
in `voxd/config.py` are **deleted** (forward cutover). `_TokenRedactFilter` and
`_install_token_redact_filter` (uvicorn-access concern) stay in `voxd/config.py`.

---

## 4. No-silent-gaps logging map

Which module logs which decision / transition / outcome at which level. "Log at
the decision point, once" — no per-iteration, no redundant cross-layer.

| Gap (audit §2b) | Where it is logged | Level | Line (rendered) |
|---|---|---|---|
| **Program mode transition** (was: nothing) | `voxd/programs/control_channel.py` — one choke point: `apply_next` reads `source.mode` before/after each applied signal and logs **only on change** | INFO | `music: generating_first → playing_filling (celtic)` |
| **Generation success** (was: only failure) | `voxd/programs/fill_recorder.py::ready` — symmetric with the existing failure paths | INFO | `music: generated part 3 of 12` |
| generation permanent failure | `fill_recorder.py::permanent`/`_failed` | WARNING | `music: part 4 failed permanently: bad_prompt` |
| generation transient failure | `fill_recorder.py::transient` (retries; not yet user-actionable) | DEBUG | `music: part 4 transient failure, backing off` |
| **Provider selection** (was: invisible) | `providers/__init__.py::ProviderRegistry` — logged **deduplicated** (only when the decision changes from the last logged one), so a stable choice is ~1 line per process | INFO | `provider: auto-detected polly (AWS credentials valid)` |
| **AWS-probe failure** (was: swallowed at `:156`) | `providers/__init__.py::_has_aws_credentials` — folded into the auto-detect decision line above, or its own DEBUG when Polly is *not* chosen | DEBUG | `provider: polly not chosen (aws probe: nonzero exit)` |
| **Malformed hook payload** (was: `hooks.py:111`) | `hooks.py::_read_hook_input` | WARNING | `hook: malformed JSON payload on stdin (214 bytes); treated as empty` — **byte count only, never content** |
| **Config-absent hook** (was: `hooks.py:439/464`) | the `config_dir is None` branches | DEBUG | `hook stop: no vox config for cwd; staying silent` (see **D5** — DEBUG, not INFO, to avoid a line on every hook in every non-vox repo) |
| **Auth rejection** | `voxd/router.py:64` — **already present** (q637); confirmed correct: metadata-only WARNING, no token | WARNING | `Auth rejected: connection from 127.0.0.1:…` |
| **Synthesis outcome at its own layer** | `voxd/synthesis.py` — the one user-readable INFO per speech event (see §5) | INFO | `synthesized 5 chars with openai, cached` |

The Program mode-transition choke point is the design's key structural call: one
logger in `ControlChannel.apply_next` captures **every** transition — user-driven
and automatic (first-track-ready, pool-full) — uniformly, instead of scattering
loggers across a dozen signal/handler files. Mode transitions are intrinsically
low-volume (a track *advance* within `rotating` does not change mode), so this
never becomes per-iteration noise.

---

## 5. Noise → DEBUG, and the INFO rewrites

### 5.1 Seven-lines-per-chime → one

| Line | Module | Change |
|---|---|---|
| `Client connected (total: N)` | `router.py:70` | → **DEBUG** (transport) |
| `Client disconnected (total: N)` | `router.py:113` | → **DEBUG** |
| `Playback start: X` | `voxd/playback.py:438` | → **DEBUG** |
| `Playback spawn: cmd=… audio_env={…} timeout=…` | `voxd/playback.py:250` | → **DEBUG** (the dump) |
| `Playback ok: elapsed=… file=… size=…` | `voxd/playback.py:414/423` | → **DEBUG** |
| `Playback done: X` | `voxd/playback.py:441` | → **DEBUG** |
| `Dedup: skipping duplicate chime` | `system_handlers.py:69` | → **DEBUG** |
| `Chime: %s` | `system_handlers.py:73` | **stays INFO**, rewritten → `played chime: done` |

Result: **one** INFO line per chime (`played chime: done`), plus ERROR/WARNING on
a real failure (`Playback FAILED`/`SUSPICIOUS` in `playback.py` stay at their
current higher levels — they are genuine faults, not noise).

### 5.2 INFO rewrites (audit before→after table)

| Now (developer format) | After (plain sentence) | Module |
|---|---|---|
| `cache MISS: id= provider=openai voice= size=566 chars_in=5 not cached` | `synthesized 5 chars with openai, cached` | `voxd/synthesis.py:353` |
| `cache HIT: id= provider=openai voice= file=… chars_in=5` | `synthesized 5 chars with openai (cache hit)` | `voxd/synthesis.py:297` |
| `Direct-play ok: provider=say voice= elapsed=0.512s chars=12` | `spoke 12 chars with say (0.5s)` | `voxd/synthesis.py:430` |
| `Session config: notify=c speak=n voice=roger provider=None vibe_mode=auto` | `ready — voice roger, chimes only, auto vibe` | `server.py:895` |
| `Playback spawn: cmd=[…] audio_env={…}` | *(dump → DEBUG; the one INFO is the chime/synth line above)* | `voxd/playback.py` |
| `voxd environment: pid=… env={…}` | short `voxd starting (pid N, v4.12.4)`; the env dict → DEBUG | `voxd/config.py:309` |
| `Config: set voice = 'roger' …` ×4 per vibe change (one per field) | one `vibe set: auto (3 fields updated)`; per-field → DEBUG | `frontmatter.py:117-119` |

Rendering `notify=c speak=n vibe_mode=auto` as `chimes only, auto vibe` needs a
small internal-code→phrase translator. It lives beside the session-config INFO in
`server.py` (a private `_session_summary_sentence()` helper) — no new module for
one formatter (PY-OO-2 keeps it where its only caller is).

### 5.3 Framework-logger suppression + vox-owned per-tool line

- Suppress the `mcp` framework request logger in the client `dictConfig`
  `loggers` section: `"mcp": {"level": "WARNING"}`, `"mcp.server":
  {"level": "WARNING"}`, `"mcp.server.lowlevel": {"level": "WARNING"}`. Kills the
  38× `Processing request of type CallToolRequest` (no tool name) noise.
- Replace it with a **vox-owned per-tool INFO line**. The 11 `@mcp.tool()`
  functions in `server.py` get one shared wrapper (`@_logged_tool`) that logs
  `mic:<tool> — <metadata summary>` at INFO on entry — e.g. `mic:unmute — 5
  chars`, `mic:music on`, `mic:vibe auto`. Metadata only: char counts, mode
  names, never the spoken text. The wrapper is a thin decorator applied at each
  `@mcp.tool()` site; it does not change tool signatures.

### 5.4 Rotation for the append-only files

- **`vibe-trace.log`** → gains size-capped rotation by composing `AtomicAppendLog`
  (§6). Lossless for vibe-trace because each `record()` opens a fresh fd (no
  held descriptor to signal on rename).
- **`vox-fallback.log`** (new) → same `AtomicAppendLog` rotation.
- **`vox-boot.log`** (new, §7) → same.
- **`hook-errors.log`** → **deleted**. It was the hook shell `2>>` redirect
  duplicating `tts.log` warnings. With consolidation, hook stderr is discarded
  by Claude Code anyway and warnings ship to `vox.log`. Remove the redirect from
  the hook scripts; delete the path (forward cutover).

---

## 6. Routed item 1 — daemon-startup crash-landing

`voxd/daemon.py::main` today calls `daemon_cfg.configure_logging()` **before**
`CrashLogger(logger).install_excepthook()`, and daemon stderr was removed in
q637. If `configure_logging` itself raises (ENOSPC / EACCES / a symlink at the log
path caught by `O_NOFOLLOW`), the failure lands in a void: no `vox.log` handler
yet, no `CrashLogger`, no stderr. The comment at `daemon.py:345-346` — "before any
startup step can raise" — is false.

**Design: an emergency landing installed before, and independent of, the log
handler.**

Add `CrashLogger.install_bootstrap_excepthook(emergency_path)`. It sets
`sys.excepthook` to a handler that writes `traceback.format_exception(...)` (each
physical line SANITIZER-escaped) to `emergency_path` via `AtomicAppendLog` —
which depends only on `os.open`/`os.write`, **not** on `dictConfig` or the
logging subsystem. So even a total logging-construction failure is captured.

`main()` sequence becomes:

```text
1. CrashLogger(logger).install_bootstrap_excepthook(log_dir()/"vox-boot.log")  # FIRST, no deps
2. ensure_user_dirs()                    # may raise → caught → vox-boot.log
3. configure_daemon_logging()            # may raise → caught → vox-boot.log
4. CrashLogger(logger).install_excepthook()   # upgrade to vox.log for the rest
5. … load keys, token, wire subsystems, run …
```

`vox-boot.log` is a dedicated, near-empty file (written only on a catastrophic
boot failure), 0600, rotated like the other append-only sinks. Writing to a
*different path* from `vox.log` avoids any interaction with the possibly
half-constructed rotating handler. The false comment at 345-346 is rewritten to
state the actual guarantee (bootstrap hook covers construction; the logger hook
upgrades once the file handler is live).

---

## 7. Routed item 2 — log-dir 0700 re-tighten

Covered in §3: `configure_client_logging` and `configure_daemon_logging` both
call `PrivateState(log_file).ensure_private_tree()` before `dictConfig`, which
creates and `chmod(0o700)`s `~/.punt-labs/vox` and `…/logs` on **every** call —
so the client entry points (`server.py`, `hooks.py`, `cli_io.py`, `playback.py`),
which never called `ensure_user_dirs()`, now re-tighten a pre-existing loose
(0755) dir. Files were already 0600 via `PrivateRotatingFileHandler` /
`AtomicAppendLog`; this closes the *directory* bit. Client tightening is
best-effort-with-WARNING (never crash a hook); the daemon keeps its fail-closed
`ensure_user_dirs()` gate.

---

## 8. Write-set (the implementation contract)

### Create

| File | Why |
|---|---|
| `src/punt_vox/append_log.py` | `AtomicAppendLog` — the reusable multi-writer-safe sink: `PrivateState`-guarded, SANITIZER-escaped, 0600, single-`os.write` atomic `O_APPEND`, size-capped rename rotation. Generalizes `vibe_trace`. Errors swallow-to-stderr (never re-enter logging). |
| `src/punt_vox/log_wire.py` | `LogRecordWire` (frozen dataclass) + `to_message`/`from_message` — the `{"type":"log",…}` schema, shared by client shipper and daemon sink (PY-IC-9: wire types in their own module). |
| `src/punt_vox/log_ship.py` | `DaemonLogHandler(logging.Handler)` + `LogShipper` (singleton: bounded deque, `attach`/`detach`/`flush(ws)` coroutine, `drain_to_fallback` for `atexit`, drop counter). |
| `src/punt_vox/voxd/log_sink.py` | `LogHandler(MessageHandler)` — receives `{"type":"log",…}`, reconstructs a role-tagged `LogRecord` with `args=None`, `handle()`s it into `vox.log`. Rejects a malformed log frame with a metadata-only WARNING. |

### Modify

| File | Change |
|---|---|
| `src/punt_vox/logging_config.py` | Become the unified owner: `configure_daemon_logging()` + `configure_client_logging(*, role, verbose)`; shared constants/format/suppression (incl. `mcp*`); `PrivateState.ensure_private_tree()` dir re-tighten. Delete old `configure_logging`. |
| `src/punt_vox/voxd/config.py` | Delete `DaemonConfig.configure_logging`, `_warn_on_loose_logs`, logging constants (forward cutover). Keep `_TokenRedactFilter`. Demote `log_environment` env-dump to DEBUG; keep a short INFO `voxd starting (pid, version)`. |
| `src/punt_vox/voxd/daemon.py` | `main()`: install bootstrap excepthook FIRST; call `configure_daemon_logging()`; upgrade excepthook after; register `LogHandler` in `_build_handler_dict` (`"log"`); fix the false 345-346 comment. |
| `src/punt_vox/voxd/crash_logging.py` | Add `install_bootstrap_excepthook(emergency_path)` writing via `AtomicAppendLog`. |
| `src/punt_vox/voxd/router.py` | Client connect/disconnect INFO → DEBUG. (Auth-reject WARNING already correct.) |
| `src/punt_vox/voxd/playback.py` | `Playback start/spawn/ok/done` INFO → DEBUG. Keep FAILED=ERROR, SUSPICIOUS=WARNING. |
| `src/punt_vox/voxd/system_handlers.py` | `ChimeHandler`: the one INFO rewritten `played chime: <signal>`; dedup line → DEBUG. |
| `src/punt_vox/voxd/synthesis.py` | Rewrite cache-HIT / cache-MISS / Direct-play-ok INFO to the plain sentences (§5.2); this is the one INFO per speech event. |
| `src/punt_vox/voxd/programs/control_channel.py` | Log Program mode transitions at INFO in `apply_next` (one choke point, on-change only). |
| `src/punt_vox/voxd/programs/fill_recorder.py` | `ready` → symmetric success INFO; `permanent`/`_failed` → WARNING; `transient` → DEBUG. |
| `src/punt_vox/providers/__init__.py` | `ProviderRegistry`: deduplicated provider-selection INFO; AWS-probe outcome folded in / DEBUG. |
| `src/punt_vox/hooks.py` | `_hook_callback` → `configure_client_logging(role="hook")`; malformed-payload WARNING (byte count only); config-absent DEBUG line. |
| `src/punt_vox/server.py` | `configure_client_logging(role="mcp")`; session-config INFO rewrite + `_session_summary_sentence()`; `@_logged_tool` per-tool INFO wrapper on the 11 tools; `mcp*` suppression via the unified config. |
| `src/punt_vox/cli_io.py` | `configure_client_logging(role="cli", verbose=self._verbose_seen)`. |
| `src/punt_vox/playback.py` | Detached worker `__main__` → `configure_client_logging(role="playback")`. |
| `src/punt_vox/vibe_trace.py` | Refactor `VibeTraceLog` to compose `AtomicAppendLog` (dedupe the O_APPEND logic; gain rotation; error path now swallow-to-stderr). |
| `src/punt_vox/frontmatter.py` | Per-field vibe writes → DEBUG; one INFO summary per vibe change. |
| `hooks/notify.sh`, `hooks/signal.sh`, `hooks/notify-permission.sh` (+ any `2>>` sites) | Remove the `2>> …/hook-errors.log` redirect (delete the path). |
| `CLAUDE.md` | Fix stale module map: `hooks.py` row + line ~159 reference `classify_signal()` / `resolve_tags_from_signals()` — neither exists (grep = 0 hits). COO-owned edit; flagged here. |
| `docs/architecture.tex` | Update the sink-map / logging section to the consolidated model (one `vox.log`, ship-over-WS, fallback). |
| `CHANGELOG.md` | `## [Unreleased]` entry under Changed/Fixed/Security. |

### Delete (forward cutover)

- `tts.log` writer path — the client `PrivateRotatingFileHandler` install (folded
  into the `logging_config.py` rewrite; no client writes a file anymore).
- `DaemonConfig.configure_logging` + `_warn_on_loose_logs` + logging constants.
- `hook-errors.log` shell redirect.

### Tests (mirror source; PL-PL-3 / PY-BS-6)

`tests/test_append_log.py`, `tests/test_log_wire.py`, `tests/test_log_ship.py`,
`tests/test_voxd_log_sink.py`; extend `test_logging_config.py`,
`test_voxd_config.py`, `test_crash_logging.py`, `test_control_channel.py`,
`test_fill_recorder.py`, `test_providers.py`, `test_hooks.py`,
`test_voxd_system_handlers.py`, `test_voxd_playback.py`, `test_vibe_trace.py`.

---

## 9. Test plan — modeled properties asserted by name

| Property | Test | Assertion |
|---|---|---|
| **multi-writer safety** | `test_append_log.py::test_concurrent_appends_never_interleave` | N processes each append M distinct lines to one file → file has exactly N·M lines, every line intact (no torn/interleaved bytes). |
| multi-writer safety (fallback) | `test_log_ship.py::test_fallback_is_multiwriter_safe` | two `LogShipper`s in separate processes fall back to one `vox-fallback.log` → all lines present and whole. |
| **daemon-down fallback** | `test_log_ship.py::test_daemon_down_records_go_to_fallback` | `flush` with a failing/absent ws → buffered records land in the 0600 fallback file; none lost. |
| daemon-down fallback (never opened) | `test_log_ship.py::test_atexit_drains_to_fallback` | client that never connected → `drain_to_fallback` writes the tail. |
| bounded buffer | `test_log_ship.py::test_deque_drops_oldest_and_counts` | > maxlen records → oldest dropped, drop-count line written to fallback. |
| **gap: mode transition** | `test_control_channel.py::test_mode_transition_logged_once` | `caplog` at INFO shows exactly one `generating_first → playing_filling` on first-track-ready; a same-mode advance logs none. |
| **gap: generation success** | `test_fill_recorder.py::test_ready_logs_success_symmetrically` | `ready()` emits an INFO line; paired with the existing failure line at WARNING. |
| **gap: provider selection** | `test_providers.py::test_auto_detect_logs_decision_once` | INFO decision line present; a second call with the same env logs nothing (dedup). |
| gap: aws probe failure | `test_providers.py::test_aws_probe_failure_logged` | nonzero/timeout probe → a DEBUG line records why Polly was skipped. |
| **gap: malformed hook payload** | `test_hooks.py::test_malformed_payload_warns_metadata_only` | garbled stdin → one WARNING with a byte count and **no payload content**. |
| **gap: config-absent hook** | `test_hooks.py::test_config_absent_logs_debug` | `config_dir is None` → one DEBUG line (see D5). |
| **≤1 INFO per event: chime** | `test_voxd_system_handlers.py::test_chime_emits_single_info` | `caplog` INFO count == 1 across the whole chime path. |
| **≤1 INFO per event: speech** | `test_voxd_synthesis.py::test_synthesize_emits_single_info` | INFO count == 1; message is the plain sentence. |
| **emergency landing** | `test_crash_logging.py::test_bootstrap_hook_lands_without_dictconfig` | raise before any `dictConfig` → traceback appears in `vox-boot.log`. |
| **dir re-tighten** | `test_logging_config.py::test_client_config_tightens_loose_log_dir` | pre-create `logs/` at 0755 → `configure_client_logging` leaves it 0700. |
| framework suppression | `test_logging_config.py::test_mcp_framework_logger_suppressed` | `mcp.server.lowlevel` at INFO does not reach the handler. |
| wire round-trip / injection | `test_voxd_log_sink.py::test_shipped_message_not_reinterpolated` | a `message` containing `%s`/newline is emitted verbatim (escaped), never re-interpolated, one physical line in `vox.log`. |

---

## 10. Open decisions — need an operator ruling before implementation

**D1 — Rename `voxd.log` → `vox.log`?**
The consolidated file now carries both daemon and client lines, so "voxd" (the
daemon process) undersells it. *Recommend: rename to `vox.log`* (clean forward
cutover — no user base to migrate; better matches the operator's user-readability
thesis; a user greps "the vox log"). Fallback = `vox-fallback.log`, boot =
`vox-boot.log`. Cost: churn in `doctor.py`, tests, `docs/architecture.tex` that
name `voxd.log`. Alternative: keep `voxd.log` to minimize churn.

**D2 — MCP-server log latency.**
The long-lived MCP server buffers client log records in-memory until its next WS
tool call (or `atexit`); between calls (possibly minutes) they are not yet
durable. *Recommend: accept it* — diagnosis-not-monitoring, bounded deque,
`atexit` + fallback catch the tail; no background flusher thread (YAGNI).
Alternative: a periodic background flush task (adds a timer/thread to a thin
client).

**D3 — Provider-selection dedup granularity.**
*Recommend: log the decision at INFO, deduplicated per distinct decision* (~1
line per process lifetime, durable). Alternatives: once-per-process regardless,
or every-call-at-DEBUG (not durable → reintroduces the gap).

**D4 — Append-only rotation race.**
`AtomicAppendLog` rotates by best-effort rename-on-oversize, which has a rare
double-rename race under concurrent writers — bounded and effectively never for
these low-volume sinks (`vibe-trace.log` reached 62 KB over the project's life).
*Recommend: accept the best-effort rename*, documented. The lossless alternative
(rotate-by-rename + signal writers to reopen) is overkill for fallback/trace
sinks whose writers hold no long-lived fd.

**D5 — Config-absent hook line: DEBUG or INFO?**
*Recommend: DEBUG.* At INFO it would print on **every** hook event in every
non-vox repo (hooks are globally installed) — that is noise, and the standard
treats an already-implied no-op as noise. The config-*present*-but-skip cases
(`notify=n`) already log at INFO; only the wiring-diagnosis case (config absent)
goes to DEBUG. Operator may prefer it durable at INFO if hook-wiring debugging
outweighs the per-event noise.

**D6 — No Z model for the fallback selector.**
*Recommend: no Z model* (§2.5): a 2-state, per-record, stateless, self-correcting
I/O selector with no cross-transition invariant, atop an already-Z-modeled
Program machine. Ratify so implementation proceeds without the ceremony.

---

## 11. Operator rulings & design amendments (2026-07-19)

Leader design review complete; operator ruled on the open decisions. This
section is authoritative where it differs from §10.

**D1 — RATIFIED: rename `voxd.log` → `vox.log`.** The design already targets
`vox.log`; this confirms it. Implementation must update every reference to the
old name — `doctor.py`, tests, `docs/architecture.tex`, any `service.py`/plist
mention — forward cutover, no alias to the old name. Fallback = `vox-fallback.log`,
boot = `vox-boot.log`.

**D2 — AMENDED: add a background flush task** (operator overrode "accept
buffering"). The long-lived MCP server must make its buffered log records durable
within **seconds**, not only on the next tool call / `atexit`. Add a lightweight
periodic flusher to `LogShipper` for long-lived clients:

- A small background flush (e.g. a daemon thread or an asyncio task owned by the
  MCP server) that drains the deque to the daemon every ~2 s while the process
  lives. Keep the existing on-`connect()` / on-`close()` flushes.
- It must never block the hot path and must fall back to `AtomicAppendLog` on a
  send failure exactly like the synchronous flush. Short-lived clients (hooks,
  CLI, detached playback) exit fast and do not need it — gate the flusher to the
  long-lived server role so a hook doesn't spawn a thread it never uses.
- Clean shutdown: the flusher stops and does a final drain on `close()`/`atexit`.

**D3 — RATIFIED** (provider-selection INFO, deduplicated per distinct decision).
**D4 — RATIFIED** (best-effort rename-on-oversize rotation for the append sinks).
**D6 — RATIFIED: no Z model.** Leader concurs with §2.5 — a stateless 2-state
per-record I/O selector with no cross-transition invariant; the observed Program
machine is already modeled in `docs/audio-programs.tex`. Adding log-line observers
does not change that state space. No `/z-spec` ceremony for this unit.

**D5 — AMENDED: a configurable local log level (the "humble option").** Default
stays quiet; a user raises verbosity only when debugging.

- Add a `log_level` config key to `config.py` (implementer chooses the
  `DURABLE_KEYS`/`EPHEMERAL_KEYS` routing and states why; a debug toggle a user
  flips "when needed" leans ephemeral/`vox.local.md`, but durable is acceptable if
  it reads more naturally). **Default absent → INFO** (the quiet, plain-sentence
  level). Accept at least `info` and `debug`.
- `configure_client_logging` and `configure_daemon_logging` read `log_level` and
  set the handler/root level accordingly (generalizing the existing `verbose`
  param — `--verbose` on the CLI is equivalent to a one-shot `debug`). Client
  processes (hooks, CLI, detached) re-read config each run, so a level change
  applies to them immediately; the daemon reads it at startup, so a running
  daemon picks up a change on `vox daemon restart` (document this — do NOT build a
  live-reload channel; most DEBUG detail the operator wants is client-side
  anyway: the config-absent hook line, per-call dumps, transport lines).
- **Config-absent hook line stays DEBUG** (§4 row unchanged): hidden at the
  default INFO level, visible once the user raises `log_level` to `debug`. This
  satisfies both halves of the thesis — no line on every hook in every non-vox
  repo by default, but fully discoverable when needed.
- Add a **humble setter**: extend the `/vox` command family and the `vox` CLI with
  a `log` sub-action (e.g. `/vox log debug` / `/vox log info`, and the CLI
  equivalent). Keep it low-key in help text — a debugging aid, not a headline
  feature. `mic:status` should report the current `log_level` so it is
  discoverable.

**Leader review findings — REQUIRED in implementation (not optional):**

- **Finding A — framing-interleave test.** Log frames now share the `/ws` socket
  with real RPCs (flushed on connect, before the request frame, and now
  periodically by D2's flusher). Add a test proving **a real tool/RPC call still
  receives its correct response when N log frames are buffered and flushed on the
  same connection** — including a mid-life periodic flush landing between RPCs.
  The §9 plan proves log properties but never proves the RPC path is unharmed;
  that is the one place a bug corrupts a real request, not just a log line.
- **Finding B — connection-lifecycle + vibe_trace health.** Verify the actual
  `VoxClientSync`/`_VoxdTransport` connection model (per-call vs persistent) and
  make the flush cadence (on-connect / on-close / periodic) match it; document
  the real cadence in code. Separately, the `VibeTraceLog` → `AtomicAppendLog`
  refactor MUST preserve the existing `mic:status` vibe-trace **health**
  surfacing (`is_writable()`/`health()`); keep a test asserting `mic:status`
  still reports vibe-trace sink health after the refactor.

**CLAUDE.md is COO-owned** — the implementer must NOT edit `CLAUDE.md` (the stale
`classify_signal` module-map fix); the leader makes that edit directly before the
PR. Everything else in the §8 write-set stands.
