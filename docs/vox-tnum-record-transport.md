# vox-tnum — record audio-return transport

## Decision

`record` stops returning audio over the WebSocket. The daemon writes the
synthesized MP3 to the caller's destination on disk and returns the **path**
plus a byte count. No audio crosses the wire. This is candidate **C**
(daemon-writes-file, returns path) from the mission brief.

The daemon already owns every other audio file on disk — the content-addressed
cache, ephemeral synthesis temp files, and saved music tracks. `record`'s whole
purpose is to produce a file; today it reads that file back into memory,
base64-encodes it (a 33% size inflation), ships it across the socket, and the
client base64-decodes and writes it to disk again — a full round-trip through
memory and the wire for bytes that started and end as a file on the same
filesystem. Removing the byte transfer removes the defect class, not just the
current symptom.

## Trust boundary this design relies on

voxd clients are **local, same-user processes sharing one filesystem** with the
daemon. Browser-origin handshakes are already rejected (CSWSH/DNS-rebinding
defense, `router.py`). The daemon runs as the same user that invoked the client,
so a path the daemon can write is a path the user could write directly — writing
a recording to a client-named absolute path (or into a client-named directory)
is within the daemon's existing filesystem authority and grants no new
capability. A path or permission failure (`ENOENT` parent, `EACCES`) surfaces as
a normal error reply, never a crash.

Because no audio bytes traverse the WebSocket, there is **no receive buffer to
bound and no frame cap to raise**. The unbounded-buffer exposure the brief warns
against is eliminated by construction — there is no buffer. This is why C is
strictly safer than raising the client's `max_size` (candidate B, which just
moves the cliff and reintroduces a large receive buffer) and than streaming
(candidate A, which still reassembles bytes in the client).

**Explicitly out of scope:** `record` against a *remote* daemon (bound to a
non-loopback host over `VOXD_HOST`/SSH). Path-return assumes a shared
filesystem; a path on the daemon's host is meaningless to a remote client.
Remote record of audio above ~1 MiB does not work today either (the same 1009
ceiling), so no working behavior is lost. If remote byte-return is ever a real
requirement it is a separate streaming transport — not a compatibility shim on
this one.

## The four defects and how C fixes each

1. **Transport (root cause).** The daemon sent the whole base64 payload as one
   `websocket.send_json` frame (`speech_handlers.py:276`). The client connects
   with the `websockets` default `max_size=1 MiB` (`client.py:179`), so a
   3,072,047-byte frame is rejected with close 1009. Under C the terminal reply
   is `{"type":"audio","path":...,"bytes":N}` — a few hundred bytes regardless
   of audio length. The ceiling is gone because the payload is gone.

2. **Timeout.** `record` waited on a single frame with a fixed 30 s timeout
   (`_TIMEOUT_SYNTHESIS`, `client.py:442`); a fresh 6000-char ElevenLabs
   synthesis took 2 m 04 s, so the client abandoned the call while the daemon
   was still synthesizing. Fix: the daemon sends an immediate `recording` ack
   the moment it accepts the job, then the client waits for the terminal `audio`
   frame under a **length-scaled deadline** (`_RECORD_TIMEOUT_BASE +
   _RECORD_TIMEOUT_PER_CHAR * len(text)` = 60 + 0.05·len — 6000 chars → 360 s,
   comfortably above the observed 124 s), **capped at `_RECORD_TIMEOUT_MAX = 600
   s`**. The wait is proportional to the work and never fixed, so the reported
   defect — a fixed 30 s deadline firing on a normal multi-minute synthesis — is
   fixed. The 600 s cap is a hung-daemon backstop: it bounds the client's wait so
   a wedged daemon is detected within ten minutes rather than never. It does not
   abandon legitimate work in practice, because `core.py` splits a long record
   into sentence-boundary chunks synthesized **in parallel**, and every provider
   caps single-request input length — so real wall-clock for even a
   tens-of-thousands-char record stays a few minutes, far below the cap. The one
   bound this trades for: a single synthesis that genuinely ran past 600 s would
   be abandoned client-side (a one-line timeout error) while the daemon, which
   has no matching server-side deadline, finishes and lands the file — an
   orphaned successful write. That case does not arise under parallel chunking +
   provider input caps; if it ever does, the fix is a daemon-side synthesis
   deadline so client and daemon agree, not a larger client cap. The ack also
   separates "daemon never accepted the job" (fast failure) from "synthesis is
   legitimately long" (slow success).

3. **CLI error mapping.** A `websockets.exceptions.ConnectionClosedError` from
   `ws.recv()` escaped the transport (`send_and_recv` catches only
   `TimeoutError`, `client.py:226`), propagated past the record command's
   `(VoxdConnectionError, VoxdProtocolError)` catch (`__main__.py:444`), and
   printed a raw rich traceback. Fix: the transport wraps every
   `websockets` `ConnectionClosed`/`WebSocketException` and `OSError` raised from
   `ws.recv()`/`ws.send()` into `VoxdConnectionError`/`VoxdProtocolError` — the
   two types every CLI command already catches — so any transport failure is a
   one-line error. This is the general fix; it holds even though C removes the
   specific big-frame close.

4. **Daemon-side crash on client close.** When the client rejected the big frame
   and closed, the daemon's in-flight `send_json` raised `AssertionError` in the
   uvicorn/websockets drain path, logged as `uvicorn.error: data transfer
   failed`. Fix: C removes the big frame, so the interrupt window nearly
   vanishes; additionally the record reply is guarded exactly as
   `SynthesizeHandler` guards its terminal reply — `contextlib.suppress(
   WebSocketDisconnect, RuntimeError)` around the send (`speech_handlers.py:215`)
   — so a client vanishing during the long synthesis window (between the
   `recording` ack and the `audio` reply) ends the request cleanly at debug-log
   level and never corrupts the daemon's send state.

## Invariants (the design must satisfy every one)

1. `vox record` of arbitrarily long text delivers the **complete, byte-correct**
   MP3 to the requested output path. Verified client-observably: the daemon
   reports `bytes=N`; the client stats the output file and asserts its size
   equals `N`.
2. **No silent size ceiling.** Audio never crosses the wire, so there is no
   single-frame cap and no transfer size limit within the trust boundary.
3. A **legitimate long synthesis is not abandoned** by a premature *fixed*
   client timeout: the deadline is length-scaled after an immediate ack
   (`60 + 0.05·len(text)`), capped at 600 s as a hung-daemon backstop. Parallel
   chunking + provider input caps keep real wall-clock well under the cap, so no
   realistic record is abandoned; a synthesis genuinely exceeding 600 s is the
   one documented bound (see the Timeout section).
4. **All client-facing transport failures are one-line CLI errors**, never a
   traceback (transport wraps `websockets`/`OSError` into `VoxError` subtypes).
5. A client **disconnecting mid-transfer never crashes the daemon** or corrupts
   its connection/send state (guarded reply; no large in-flight frame).
6. **No unbounded-buffer exposure** beyond the local same-user boundary — there
   is no audio buffer in the client's receive path at all.
7. **No migration/compat/shim/bridge code.** The old single-frame `{"type":
   "audio","data":<base64>}` return path is deleted, not aliased. `VoxClient.
   record` changes shape and every caller changes with it (forward integration,
   PY-RF-6).

## Mechanism

### Wire protocol

Request (client → daemon), one text frame of JSON:

```json
{"type":"record","id":"<hex>","text":"<...>",
 "output_dir":"/abs/dir",
 "output_path":"/abs/file.mp3",
 "...spec fields": "voice, provider, model, rate, language, stability, ..."}
```

The request always carries `output_dir` (an absolute directory) and, optionally,
`output_path` (an absolute explicit file) as a pin. When `output_path` is
present the daemon writes exactly there; when it is absent the daemon names the
file `generate_filename(text)` under `output_dir` — the same content-addressed
name every other MP3 uses, computed daemon-side from the text it already holds.
Both fields are **absolute**: the client resolves them against the caller's cwd
before they go on the wire, and the daemon **rejects a non-absolute path with an
error frame** — `voxd`'s cwd is not the caller's shell, so a relative path is a
protocol violation, not a directory to resolve. The daemon requires
`output_dir`; a request without it is an error.

Replies (daemon → client), two text frames — an immediate `recording` ack
followed by the terminal `audio` frame carrying the final path and byte count:

```json
{"type":"recording","id":"<hex>"}
{"type":"audio","id":"<hex>","path":"/abs/file.mp3","bytes":12345}
```

Failure at any point is the existing `{"type":"error","id":...,"message":...}`
frame, which the transport already turns into `VoxdProtocolError`
(`client.py:_decode`).

### Daemon (`RecordHandler`)

1. Parse the request; reject empty text, a missing `output_dir`, and any
   non-absolute `output_dir`/`output_path` — each with an error frame.
2. Send the `recording` ack.
3. `synthesize_to_file(text, spec)` → `SynthesisOutcome(path, cached)`
   (unchanged; may take minutes).
4. Place the audio at the destination **atomically**: write to a sibling temp
   file in the destination directory, then `os.replace` it onto the final path,
   so a crash or error mid-write leaves **no partial file** (invariant 1). For a
   cache hit the source is the cache file (copy, never move — the cache keeps its
   entry); for a fresh synthesis the ephemeral temp is moved. The daemon then
   removes any ephemeral source exactly as it does today
   (`speech_handlers.py:273`).
5. Reply with the terminal `audio` frame carrying the final absolute path and
   the byte count, guarded by `suppress(WebSocketDisconnect, RuntimeError)`.

Filename generation and the destination write move **into the daemon** — the
daemon becomes the single owner of "produce a recording file," matching the
architecture's daemon/client boundary (the daemon owns files on disk; clients
are thin).

### Client (`VoxClient.record` / `VoxClientSync.record`)

Signature changes from `record(text, spec) -> bytes` to:

```python
record(text, spec, *, output_path: Path | None = None,
       output_dir: Path | None = None) -> RecordResult
```

`RecordResult` is a small frozen value type `(path: Path, bytes: int, cached:
bool)`. The method sends the request via `send_and_drain(msg,
timeout=<length-scaled>, terminal_type="audio")`, reusing the existing
ack-then-terminal drain that `synthesize` already uses. The base64 decode path
(`client.py:447-451`) is deleted.

## Does any other client call share this ceiling?

Only `record` returns audio bytes. `synthesize` and `chime` play locally on the
daemon and return small status frames; `voices`, `health`, and every `program_*`
call return small JSON. **Both** record callers — the CLI `record` command
(`__main__.py:443`) and the MCP `record` tool (`server.py:576`) — go through
`VoxClient.record`, so the single transport change fixes both. Any future
byte-returning consumer uses the same daemon-writes-file path.

## Implementation write set

Modify:

- `src/punt_vox/voxd/speech_handlers.py` — `RecordHandler`: send `recording`
  ack; write audio to the destination atomically (temp + `os.replace`); name via
  `generate_filename(text)` when only `output_dir` is given; reply `audio` with
  path + bytes; guard the reply against client disconnect.
- `src/punt_vox/client.py` — new `RecordResult` value type; rewrite `record` to
  the path-return signature over `send_and_drain(terminal_type="audio")` with a
  length-scaled timeout; delete the base64 decode. Wrap `ConnectionClosed`/
  `WebSocketException`/`OSError` from `ws.recv()`/`ws.send()` in `send_and_recv`
  **and** `send_and_drain` as `VoxdConnectionError`/`VoxdProtocolError` (fixes
  defect 3 for every call, not just record).
- `src/punt_vox/client_sync.py` — `record` signature parity, forwarding
  `output_path`/`output_dir`.
- `src/punt_vox/__main__.py` — the `record` command computes the destination
  path/dir as today but passes it to `client.record(...)` instead of writing
  bytes itself; drop `out_path.write_bytes`; assert the returned `bytes` matches
  `out_path.stat().st_size` and surface a mismatch as a one-line error.
- `src/punt_vox/server.py` — the `record` tool passes `output_path`/`output_dir`
  to `client.record(...)` and builds its result record from the returned
  `RecordResult`; it no longer receives `mp3_bytes`.

Create (specialist's discretion — recommended):

- `src/punt_vox/voxd/record_sink.py` — a small daemon-side owner of "write audio
  to an explicit path or a hashed name under a directory, atomically," so the
  handler stays thin and the write policy is unit-testable without a socket.

Delete (forward integration, no shim):

- `src/punt_vox/recording.py` (`RecordingSink`) — file writing and content-hash
  naming move daemon-side; the MCP server stops importing it. Retire
  `tests/test_recording.py` with it.

Tests: `tests/voxd/test_speech_handlers.py`, `tests/test_client.py`,
`tests/test_main.py`, `tests/test_server.py`, and a new
`tests/voxd/test_record_sink.py`.

## Test plan (cases assert invariants by name)

- **`test_long_record_delivers_byte_correct_file`** (inv. 1) — record a
  6000-char text through a fake synthesis that produces a known >1 MiB MP3;
  assert the output file exists, is a valid non-empty MP3, and
  `output.stat().st_size == reply["bytes"] == len(source_audio)`.
- **`test_record_over_frame_limit_succeeds`** (inv. 2) — audio > 1 MiB; assert
  success and that the terminal reply is an `audio` **path** frame (no `data`
  key, no frame near the old cap).
- **`test_arbitrarily_long_record_has_no_ceiling`** (inv. 2) — parametrize audio
  sizes up to several MiB; all succeed identically.
- **`test_long_synthesis_is_not_abandoned`** (inv. 3) — a fake
  `synthesize_to_file` that sleeps past the old 30 s; assert the client still
  receives `audio` under the length-scaled deadline.
- **`test_client_close_mid_transfer_does_not_crash_daemon`** (inv. 5) — client
  disconnects after the `recording` ack, before `audio`; assert the handler
  returns cleanly, no exception escapes into the router's drain, and a
  subsequent `health` request on a new connection succeeds.
- **`test_transport_failure_is_one_line_cli_error`** (inv. 4) — force a
  `ConnectionClosedError` from `ws.recv()`; assert it is wrapped as
  `VoxdProtocolError`, the CLI exits 1 with a single-line message, and no
  traceback is printed.
- **`test_no_partial_file_on_synthesis_error`** (inv. 1) — synthesis raises after
  the ack; assert the destination path does **not** exist (temp + `os.replace`
  guarantee).
- **`test_record_inherits_session_voice`** (voice ruling, see below) — session
  voice `roger`, record with no explicit voice; assert the daemon resolves and
  uses `roger`, not the provider default.

## Live-verify plan (running machine)

1. `make install`, then `vox daemon restart` (the daemon serves code from
   startup; a stale daemon tests the old path).
2. **>1 MiB record end-to-end.** `vox record -o /tmp/vox-tnum-big.mp3
   "<~6000-char text>"`. Expected: command exits 0 with the path; the `-o` file
   exists and `stat` shows > 1 MiB; `afplay /tmp/vox-tnum-big.mp3` plays the
   complete audio (ask the operator to confirm it sounds whole, not truncated).
3. **Cache hit path.** Run the same command again. Expected: near-instant, file
   written identically.
4. **Mid-transfer disconnect.** Start a long record and interrupt the client
   (Ctrl-C) during synthesis. Expected: `vox doctor` / health shows the daemon
   alive; a subsequent short `vox record` succeeds — confirming no corrupted
   send state.
5. **MCP surface.** Drive the `record` tool through the running server; expected:
   a JSON result with the file path, and the file present on disk.
6. **Error surface.** Trigger a transport failure (e.g. `vox record` while the
   daemon is stopped). Expected: a one-line error, exit 1, no traceback.

Ask the operator after each audible step — log inspection alone cannot judge
audio.

## Secondary ruling — `vox record` and the session voice

**Yes — `vox record` should inherit the session voice.** The record log showed
`voice=''` (provider default) while the session voice was `roger`. Recording is
"capture what I would hear"; a user who set `voice roger` for the session
reasonably expects the recording in `roger`. The MCP `record` tool already does
this (`voice or _session.voice`, `server.py:580`); the CLI `record` diverging is
a surprise-inducing inconsistency, and CLI `say` has the same gap.

**Where to fix (decisive):** resolve the session voice in **one** place — the
daemon. When a `record`/`synthesize` request arrives with no voice, the daemon
resolves the configured session voice from config before falling back to the
provider default. This fixes CLI `record`, CLI `say`, and every surface at once,
and lets the MCP server drop its client-side `voice or _session.voice`
daemon-default duplication (forward integration). Resolving it per-surface in the
CLI would duplicate the fallback in a third place — the wrong layer.

**Scope:** this is an independent rollback unit from the transport change and
must ship as its **own implementation mission**, not bundled with it (one
mission = one task; the transport fix and the voice-resolution fix revert
independently). Write set for that separate mission: `src/punt_vox/voxd/
synthesis.py` (or the config-resolution seam it calls) to apply the configured
voice when the request omits it, and `src/punt_vox/server.py` to drop the
duplicated client-side fallback. The `test_record_inherits_session_voice` case
above belongs to that mission.

## Z-specification assessment

**Not produced — and here is why, explicitly.** The formal-modeling gate fires
for a *stateful transfer* with 3+ transitions and invariants that must hold
across them (in-order chunk delivery, `reassembled == produced`, abort leaves no
partial file). That is candidate **A** (chunked streaming with client-side
reassembly), which was **not** chosen precisely because it carries that
complexity.

Candidate C is a linear request/response with one liveness ack:
`receive → ack(recording) → synthesize → write → reply(audio | error)`. There is
no reassembly, no accumulating buffer, no ordering invariant, no multi-frame
state the client must reconcile. The only atomicity concern — "no partial file
on failure" — is not a protocol invariant across transitions but a **single
local guarantee** provided by write-to-temp-then-`os.replace`, and the
daemon-send-safety concern (defect 4) is a defensive `suppress`, not a modeled
transition. Per the gate's own "does NOT qualify" list (single-function fixes
with no state, pure I/O helpers), C does not meet the trigger. Had A been
chosen, `docs/vox-tnum-record-transport.tex` modeling `idle → transferring →
complete/aborted` with `fuzz -t` would be required; choosing C is in part a
choice to stay below that bar.
