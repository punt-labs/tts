# Wire Host-Info Boundary — voxd client replies

Security / trust-boundary design review of the `voxd` daemon's client-facing
wire protocol. Follow-on to the vox-dvri remote-daemon audit, Category B.

Design and review only. No production code changes accompany this document.
Every fix below is enumerated so it can become an implementation bead.

## 1. The invariant — a conceptual chroot

> **The daemon is conceptually chrooted to its data root.** A path may cross the
> wire only *relative* to that root; the absolute prefix — home directory,
> username, on-disk layout — never does. A host fact with no place inside the
> jail (an installed-binary location, an environment variable, a version, the
> hostname) crosses only as a verdict, never as a value. `VOXD_TOKEN`
> authenticates a client to *use* the audio service — synthesize, play, manage
> recordings and cache, "is it up". It does not grant a map of the host.

The chroot reframes the earlier "drop every path" stance. A client legitimately
needs the *logical* location of a recording it made or a cache entry it is
clearing — `recordings/foo.mp3`, `cache/ab/cd.mp3`. That relative path is a
service fact. What it must never learn is the *prefix* that turns the logical
location into a host map: `/Users/<username>/.punt-labs/vox/…`. So most path
fields are **relativized**, not dropped; only host facts with no relative form
stay verdict-only.

Stated as a predicate over every frame the daemon sends on `/ws` or `/health`:

```text
∀ frame ∈ ClientReplies •
    ∀ p ∈ paths(frame)      • p = relativeToDataRoot(p)  -- no absolute prefix ever
  ∧ binaryInventory(frame) = ∅   -- no `which` result, no player-binary path
  ∧ identity(frame)      = ∅     -- no hostname, no username (a prefix carries it)
  ∧ versionExposed(frame) ⇒ authenticated(frame)  -- version only over the token
```

A path is "in jail" when it lies under one of the two labeled data roots
(§1.1); it crosses relativized. A path under neither root is "out of jail" and
has no relative form; it is dropped or reduced to a verdict, and the systemic
fault path (§2.6) falls back to a generic message for it.

### Threat model

The adversary holds a valid `VOXD_TOKEN`, or reaches the `/ws` socket, or probes
the unauthenticated `/health` route over the network (a remote daemon,
`VOXD_HOST` set). They cannot run code on the host. Their goal is
reconnaissance: learn the absolute prefix (home + username), the on-disk layout,
the installed binaries and versions, and the platform, to select an exploit. An
absolute prefix in any reply hands over the username and layout for free; a
`which` result and a version string narrow the CVE search.

The operator, reading the same fields locally, wants the full detail. The
resolution is not to strip detail from the system — it stays in `vox.log`, which
is `0600` and host-local — but to keep the absolute prefix out of the *wire
reply*. The relative path and the verdict cross the wire; the prefix and the raw
exception stay in the log.

### 1.1 The two data roots and the boundary helper

The daemon owns two roots. Every in-jail path lies under one of them, and each
is labeled so a client keeps the logical location without the prefix.

| Label | Root | Source | Holds |
|---|---|---|---|
| `state` | `~/.punt-labs/vox` | `paths.user_state_dir()` | `recordings/`, `cache/`, `logs/vox.log`, `run/` |
| `output` | `$VOX_OUTPUT_DIR` or `~/Music/vox` | `dirs.default_output_dir()` | saved music Programs / album parts |

Specify one helper at the wire boundary:

```text
relativize_to_data_root(path) -> (label, relpath) | None
    if path is under the state  root: return ("state",  path relative to it)
    if path is under the output root: return ("output", path relative to it)
    otherwise:                        return None   -- out of jail
```

Resolve both roots and the candidate before comparing (a `.resolve()` on each),
so a symlinked or `..`-bearing path cannot appear in-jail when it is not — the
same containment discipline `ContainmentRoot` already applies to inbound names.
A path that resolves under neither root is out of jail: the caller drops it, or
the fault path falls back to `"operation failed"`. The relative form a client
sees is the natural subdir path already — `recordings/foo.mp3`, `cache/ab/cd.mp3`,
`logs/vox.log` under `state`; the album/part path under `output` — so the label
disambiguates only when the same relative shape could sit under either root.

## 2. Every client-facing reply

Two transports carry replies: the unauthenticated HTTP `GET /health` route, and
the token-gated `/ws` WebSocket. The MCP `status` tool (`server.py`) is a
*client* of the daemon and composes its own reply; it is included because it
emits host paths of its own.

Classification key: **KEEP** (service verdict/count, crosses unchanged),
**RELATIVIZE** (in-jail path, absolute prefix stripped via §1.1), **DROP**
(out-of-jail host fact reduced to a verdict or removed).

### 2.1 HTTP `GET /health` — unauthenticated

`DaemonHealth.minimal_payload` (`voxd/health.py:66`).

| Field | Value | Class | Why |
|---|---|---|---|
| `status` | `"ok"` | KEEP | liveness verdict |
| `uptime_seconds` | float | KEEP | service metric |
| `queued` | int | KEEP | service metric (queue depth) |
| `port` | int | KEEP | the caller already dialed it |
| `active_sessions` | int | KEEP | service metric |
| `provider` | e.g. `"elevenlabs"` | **DROP** (D1 settled) | fingerprints the TTS backend to an *unauthenticated* network probe; out of jail, no relative form |

### 2.2 WS `health` — authenticated

`DaemonHealth.full_payload` (`voxd/health.py:85`). Minimal payload plus:

| Field | Value | Class | Why |
|---|---|---|---|
| `audio_env` | env-var values (`_AUDIO_ENV_KEYS`) | **DROP** (D2 settled) | env values are out-of-jail host facts and can carry secrets; verdict-drop |
| `player_binary` | `shutil.which("afplay"/"ffplay")` — absolute path | **DROP** (D2 settled) | binary inventory + install location; out of jail. Replace with a `player: present/absent` verdict if a client needs one |
| `last_playback.file` | absolute path of last-played file | **RELATIVIZE** (D2 settled) | an in-jail recording/part path → `recordings/…` or the `output` album path |
| `last_playback.stderr` | ffplay/afplay stderr | **RELATIVIZE-OR-STRIP** (D2 settled) | stderr echoes absolute paths; relativize any in-jail path token, strip the rest |
| `last_playback.rc` / `elapsed_s` / `ts` | numbers | KEEP | service verdict |
| `pid` | int | KEEP (§4) | used by `vox daemon restart`; a pid is not a host map |
| `daemon_version` | version string | KEEP — authenticated only (D3 settled) | version over the token is allowed; used by `vox doctor`. Never on the unauthenticated `/health` (already the case) |

### 2.3 WS speech / playback / store replies

| Verb | Reply frames | Field | Class | Why |
|---|---|---|---|---|
| `synthesize` | `playing`{cached}, `done`{deduped, original_played_at, ttl_seconds_remaining}, `error` | all | KEEP | verdicts/counts; no host detail |
| `record` | `recording`, `audio`{name, **path**, bytes, cached}, `error` | `path` | **RELATIVIZE** (cross-surface, §7 #2) | `str(write.path)` is the daemon recording path; the client wants the logical locator, so send `recordings/foo.mp3`, not the absolute form. `name`/`bytes` are *not* a full substitute — the client reads `path` today into `RecordResult.store_path`, so the field stays, relativized |
| `play` | `playing`, `done`, `error`, `fault` | `fault` message | **RELATIVIZE** (§2.6) | `playback failed: {failure_detail()}` appends player stderr, which carries absolute paths; relativize in-jail tokens, strip the rest |
| `fetch` | `fetch_begin`{ref, bytes, chunks, sha256}, `chunk`{seq, data}, `fetch_end`{ref, bytes}, `error`, `fault` | begin/chunk/end | KEEP | `ref` is the client's own bare name echoed back; bytes/chunks/sha256 are integrity data |
| `fetch` | | `fault` message | **RELATIVIZE** (§2.6) | `cannot read {ref!r}: {exc}` / `fetch of {ref!r} failed: {exc}` — `str(OSError)` embeds the absolute path |
| `rec_list` | `recordings`{entries:[{name, bytes}]} | all | KEEP | bare names + byte counts; the store deliberately lists no path |
| `rec_remove` | `removed`{removed:ref}, `error`, `fault` | `fault` message | **RELATIVIZE** (§2.6) | `str(OSError)` on a denied unlink embeds the path |
| `chime` | `playing`, `done`, `error`{unknown chime: signal} | all | KEEP | signal is client-supplied; no host detail |
| `voices` | `voices`{provider, voices:[…]}, `error`, `fault` | all | KEEP | voice roster is service data; the generic-exception path already replies `"operation failed"` |

### 2.4 WS cache replies

`voxd/cache_handlers.py`.

| Verb | Reply | Field | Class | Why |
|---|---|---|---|---|
| `cache_status` | `cache_status`{entries, size_bytes, **path**} | `entries`, `size_bytes` | KEEP | service counts |
| `cache_status` | | `path` | **RELATIVIZE** (cross-surface, §7 #3) | `str(info.path)` is the daemon cache dir; send `cache` (its relative form under `state`). The client reads `path` today (`CacheStatus.from_wire` `require_str`), so the field stays, relativized |
| `cache_status` | | `fault` message | **RELATIVIZE** (§2.6) | `str(OSError)` from a mid-scan stat embeds the path |
| `cache_clear` | `cache_cleared`{cleared}, `fault` | `cleared` | KEEP | delete count |
| `cache_clear` | | `fault` message | **RELATIVIZE** (§2.6) | `str(OSError)` from a denied unlink embeds the path |

### 2.5 WS program / music replies

| Verb | Reply | Field | Class | Why |
|---|---|---|---|---|
| `program_status` | `program_status`{status} | format, mode, now_playing{index, of, title}, generation{filling, attempts}, failed_parts, playback_error{part_index, kind}, name | KEEP | format-neutral verdicts/counts; `title`/`name` are manifest labels, never addresses (asserted in `status_views.py`) |
| `program_status` | | generation.`last_error`, playback_error.`reason`, failed_parts[].`reason` | **RELATIVIZE + BOUND** (D4 settled) | free-form strings from `str(exc)` (`programs/loop.py:132`, `fill_recorder.py`); relativize any in-jail path token, strip out-of-jail ones, cap the length. Raw text to the log. A closed reason-code set is a future hardening, not now |
| `program_list` | `program_list`{programs:[{id, format, style, vibe, name, ready, total, created}]} | all | KEEP | catalog metadata; `locator` is deliberately *logged only*, never sent |
| `music get` | `manifest`{album, parts:[{part, bytes}]} | all | KEEP | album name + bare part names + byte counts |
| `music new` | `generating`, `album`{album_id, parts} | all | KEEP | opaque album id + count |
| `music remove` | `removed`{album_id} | all | KEEP | opaque album id |
| program commands | `{type: <wire_type>}` | — | KEEP | acknowledgement only |

### 2.6 Fault / error frames — the systemic leak, and its improved fix

Every handler replies faults through `WireReply.error` / `WireReply.fault`
(`voxd/wire_reply.py`). Both **sanitize the log record but send `message`
verbatim on the wire** (docstring, line 71: "The wire frame carries *message*
verbatim"). The log sanitizer neutralizes control bytes for injection; it does
**not** strip an absolute prefix, and it does not touch the wire frame at all.

The consequence: any site that passes `str(exc)` where `exc` is an `OSError`
sends the OS error text straight to the client — `"[Errno 13] Permission denied:
'/Users/<username>/…/recordings/foo.mp3'"` — an absolute prefix with the
username in it. Sites that do this:

| Site | Frame | Leak |
|---|---|---|
| `cache_handlers.py:55` | `fault(str(exc))` | cache dir path |
| `cache_handlers.py:89` | `fault(str(exc))` | cache entry path |
| `rec_handlers.py:103` | `fault(str(exc))` | recording path |
| `record_handler.py:136` | `fault(str(exc))` (store write) | temp/store path |
| `record_handler.py:145` | `fault(str(exc))` (synthesis) | provider/temp path |
| `chunked_fetch.py:152` | `fault(f"cannot read {ref!r}: {exc}")` | store path |
| `chunked_fetch.py:162` | `fault(f"fetch of {ref!r} failed: {exc}")` | store path |
| `speech_handlers.py:229` | `fault(str(exc))` (synthesis) | provider/temp path |
| `speech_handlers.py:212/217` | `fault(str(result))` (direct play) | player path in the exception |
| `play_handler.py:95` | `fault(f"playback failed: {result.failure_detail()}")` | player stderr (paths) |

**The improved fix — build the message from the exception's own fields, not a
blanket generic.** The chroot makes a better answer available than "operation
failed" for the common case. At the fault boundary, given an `OSError`:

```text
rel = relativize_to_data_root(exc.filename)          -- §1.1
if rel is not None:  wire_message = f"{rel.path}: {exc.strerror.lower()}"
else:                wire_message = "operation failed"   -- out of jail, or no filename
```

So a permission fault on an in-jail recording becomes `"recordings/foo.mp3:
permission denied"` — the client sees the logical location and the cause, and
learns no prefix. A fault whose `filename` is `None`, resolves outside both
roots, or comes from a non-`OSError` falls back to the generic
`"operation failed"`. Either way the raw `str(exc)` still goes to `vox.log` via
`logger.exception`.

Client-rejection frames (`error`) that echo the *client's own* input — a hostile
`ref`, an unknown chime signal, `unknown message type: {msg_type}` in
`router.py` — are **not** host recon (the attacker supplied the value) and stay
verbatim. The rule is narrower than "sanitize all messages": **a fault message
must carry no absolute prefix — it is built from a relativized `exc.filename`
plus `exc.strerror`, or is the generic fallback.**

## 3. The MCP `status` tool (`server.py:677`)

`status` is a daemon client, not a daemon reply, but it composes host paths into
its own JSON:

| Field | Value | Class | Why |
|---|---|---|---|
| `log` | `log_health()` → `{path, writable}` | **RELATIVIZE + verdict** | `SinkHealth.path` is the absolute `vox.log` path (`append_log.py:102`). Report the relative name `logs/vox.log` plus a `healthy`/`degraded` verdict, no prefix |
| `vibe_trace` | `VibeTraceLog.default().health()` → `{path, writable}` | **RELATIVIZE + verdict** | same `SinkHealth` shape, same prefix leak; same treatment |
| `log_level`, `provider`, `voice`, `notify`, `speak`, `vibe*`, `style` | scalars/labels | KEEP | session verdicts |
| `program` / `music_mode` | daemon status (§2.5) | KEEP | already the sanitized program shape |

On a remote daemon these are the *client's* local paths, not the daemon's, but
they are still an absolute prefix in a tool reply and still name a username. The
relative name plus a `healthy`/`degraded` verdict answers "is the log working?"
without the prefix.

## 4. `vox doctor` (`doctor.py`) — verdicts and relative paths

`doctor` inspects the host and prints paths and versions throughout. In-jail
paths relativize; out-of-jail host facts reduce to a verdict. Field by field:

| Check | Current leak | Class | Fix — return |
|---|---|---|---|
| `check_ffmpeg` | `ffmpeg: {which path}` | DROP | `ffmpeg: present` / `not found — {hint}` (out of jail; drop the `which` path) |
| `check_espeak_fallback` | `{name}: {which path}` | DROP | `espeak: present (offline fallback)` / not found (out of jail) |
| `check_uvx` | `uvx: {which path}` | DROP | `uvx: present` / `not found` (out of jail) |
| `check_output_dir` | `Output directory: {abs path}` and `({OSError text})` | RELATIVIZE | `output: writable` / `not writable` — the dir *is* the `output` root, so its own name is the label; drop the raw `OSError` |
| `check_music_dir` | `Music directory: {abs path} does not exist` | RELATIVIZE | `output music dir: present/absent` — an in-jail path under the `output` root, shown relative |
| `check_claude_desktop` | `Claude Desktop config: {abs path}` | DROP | `Claude Desktop config: present` / `not found` (out of jail — under `~/Library`, neither root) |
| `check_daemon_health` | `running on port {port} (version {running_version} …)` | KEEP local | keep port + up/stale verdict; `daemon_version` is authenticated-only (D3) and printed to the *local* operator, never surfaced to a remote non-operator client |
| `check_env_overrides` | already redacts `VOXD_TOKEN` to `***` | KEEP | token already `***`; `VOXD_HOST`/`VOXD_PORT` are the caller's own config |

`doctor` today probes the **client** process's own host; running it against a
**remote** daemon (the vox-dvri case) is what turns a local diagnostic into a
remote-host map. The verdict-and-relative form is correct for both.

## 5. `vox log <level>` — wire op with an audit floor

Today `vox log` (`__main__.py:463`) writes local global config; it cannot reach
a remote daemon's level. Turning it into a wire op raises a new risk: a
token-holding client could **lower** the daemon below the level that records the
audit trail, blinding the operator to the attacker's own rejected/hostile
requests.

The audit trail lives at `INFO` and above: `Synthesize`/`Record`/`Play` info
lines, `Auth rejected` (WARNING), `rejected op` (WARNING), `operation failed`
(ERROR). The daemon's own log level must therefore never drop below `INFO`.

The wire op must clamp to a **floor of `INFO`**. The CLI enum is already
`{info, debug}` (both ≥ INFO), so the floor holds for the CLI; the wire op must
enforce the same clamp server-side rather than trust the client's value, so a
crafted frame asking for `warning`/`error` is rejected or clamped to `info`.
The op raises the daemon to `debug` and lowers it back to `info`; it can go no
lower.

## 6. Decisions — settled by the operator

All five are ruled; recorded here as the design's fixed points.

- **D1 — `/health` `provider` on the unauthenticated route: DROP.** The minimal
  HTTP payload no longer names the TTS provider to a network probe. Provider
  stays on the authenticated WS `health` and in `status`.
- **D2 — WS `health` diagnostic fields: SETTLED.** `player_binary` and
  `audio_env` are out-of-jail host facts → verdict-drop. `last_playback.file`
  is an in-jail path → relativize. `last_playback.stderr` → relativize any
  in-jail token, strip the rest. `pid`, `daemon_version`, and
  `last_playback.{rc,elapsed_s,ts}` stay (needed by `vox daemon restart` and the
  `doctor` staleness check).
- **D3 — `daemon_version` authenticated-only: YES.** Kept on the authenticated
  WS `health` (the operator's `doctor` needs it), never on the unauthenticated
  `/health` (already the case).
- **D4 — program-status free-form `reason`/`last_error`: bounded, relativized,
  out-of-jail-stripped.** Relativize any in-jail path token, strip out-of-jail
  ones, cap the length; the raw exception text goes to the log. A closed
  reason-code set is a future hardening, not this change.
- **D5 — hostname/username never in a reply: YES.** No reply emits them directly
  today; they leak only through an absolute prefix, which the chroot strips. The
  port is the only host coordinate a client needs, and it already dialed it.

## 7. Fix list (implementation beads)

Each item carries its full write-set. The `relativize_to_data_root` helper (#0)
is the shared dependency of every path fix.

0. **`relativize_to_data_root(path)` boundary helper.** New helper (a wire-layer
   module) implementing §1.1: resolve the two roots and the candidate, return
   `(label, relpath)` for an in-jail path or `None` out of jail. Unit-testable
   without a socket. Prerequisite for #1–#5, #7, #8, #10.
1. **`WireReply` fault contract.** Build the fault message from
   `relativize_to_data_root(exc.filename)` + `exc.strerror` (→
   `"recordings/foo.mp3: permission denied"`), falling back to `"operation
   failed"` for an out-of-jail/absent filename or a non-`OSError`; log the raw
   `str(exc)` via `logger.exception`. Convert every §2.6 site to this path.
   Wire-testable: assert the fault frame contains no absolute prefix and no home
   directory.
2. **`record` reply `path` — cross-surface, not daemon-only.** The client reads
   `path` today into `RecordResult.store_path` (`client.py:67,709`), so this is
   **not** shippable daemon-side alone. Write-set: daemon `record_handler.py:98`
   (send the relativized path), `client.py` (`record()` / `RecordResult` parse a
   relative path), and the `vox rec` CLI in lockstep. Testable end-to-end, not
   "without a socket".
3. **`cache_status` reply `path` — cross-surface, not daemon-only.** The client
   requires `path` today (`CacheStatus.from_wire` `require_str("path")`,
   `client.py:101`). Write-set: daemon `cache_handlers.py:57` (send the
   relativized `cache` path), `client.py` (`CacheStatus` parse a relative path),
   and the `vox cache status` CLI in lockstep.
4. **`play` fault.** Relativize in-jail path tokens in the player stderr the
   client frame carries; strip the rest; keep rc/elapsed
   (`play_handler.py:95`, `playback.py:78`). Raw stderr to the log.
5. **WS `health` payload (D2).** Drop `audio_env` and `player_binary` (verdict
   only); relativize `last_playback.file`; relativize-or-strip
   `last_playback.stderr`; keep `pid`, `daemon_version`,
   `last_playback.{rc,elapsed_s,ts}`. Daemon-side (`health.py`).
6. **`/health` `provider` (D1).** Remove `provider` from `minimal_payload`
   (`health.py:66`). Daemon-side.
7. **MCP `status` log health.** Replace `log`/`vibe_trace` `{path, writable}`
   with a relative name (`logs/vox.log`) plus a `healthy`/`degraded` verdict.
   Add a prefix-free health view alongside `SinkHealth`, or map it at the
   `status` boundary (`server.py:702-703`).
8. **`doctor` verdicts and relative paths.** Rework each §4 check: out-of-jail
   `which`/config paths → present/absent verdicts; in-jail output/music dirs →
   relative under the `output` root; drop raw `OSError` text. `doctor.py`.
9. **`vox log` wire op with an INFO floor.** New wire verb to set the daemon's
   level, clamped server-side to `≥ INFO` so a client cannot blind the audit
   trail. Reject/clamp any sub-INFO request. Model as a state op if it interacts
   with the running-level machine; otherwise a single clamped setter.
10. **Program-status reason sanitization (D4).** Relativize in-jail path tokens
    in `generation.last_error`, `playback_error.reason`, `failed_parts[].reason`;
    strip out-of-jail tokens; cap the length. Log the raw exception text. Sources
    at `programs/loop.py:132` and `fill_recorder.py`.

Items #1, #4–#10 are daemon-side (or MCP-server-side) and hold the boundary in
one place. Items **#2 and #3 are cross-surface** — daemon + `client.py` + CLI in
lockstep, because the client requires the `path` field today and must be taught
to parse the relative form. All ten hold the same invariant: an in-jail path
crosses relativized, an out-of-jail host fact crosses as a verdict, and the raw
detail stays in the log.
