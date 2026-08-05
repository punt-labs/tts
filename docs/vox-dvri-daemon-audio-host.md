# vox-dvri — the daemon is the audio host

## Decision

The daemon owns audio files and playback. Clients are thin controllers that
**never dictate a daemon-side path** and **never play remote audio locally**.
Three linked problems close together as one change:

- **P1 (vox-zu39, SECURITY).** `record` accepts a client-supplied absolute
  `output_dir`/`output_path` and the daemon writes/replaces it as the daemon OS
  user (`RecordHandler._resolve_sink` checks only `is_absolute`;
  `RecordSink.place` does `dest.parent.mkdir(parents=True)` then
  `os.replace`/copy). Across the remote trust boundary (machine B holds A's
  `VOXD_TOKEN`, per `docs/guide-remote-setup.md`) a compromised B gets arbitrary
  file overwrite on A as A's user — overwrite `~/.zshenv`, a launchd plist, an
  ssh key — i.e. likely RCE.
- **P2 (vox-ovb7).** `vox play <file>` runs the player in the **client** process
  (`__main__.py` → `playback.play_audio`, no daemon), so against a remote daemon
  it plays on B (headless) instead of A (speakers).
- **P3 (vox-eoq9).** A recording lands on A's disk, but a remote client has no
  coherent way to reference, play, or retrieve it — the returned absolute path
  is meaningless on B.

The unifying model: **the daemon is the single audio host.** Recordings live in
a daemon-owned store, addressed by a bare id/name the client never turns into a
daemon-side path. Playback runs on the daemon. Retrieval is an explicit,
opt-in, containment-checked read. The security invariant is the anchor; the two
coherence fixes fall out of it.

## Trust model (design to this)

`voxd` may serve **remote clients that hold the token but live in a different
trust domain** (machine B). The token authorizes **audio operations**. It does
**not** grant filesystem writes — or arbitrary reads — as the daemon user.

The corollary that kills the tempting shortcut: **the trust boundary is not the
network interface.** A loopback peer (`websocket.client.host == 127.0.0.1`) is
*not* proof of a same-user same-filesystem caller. The guide's own SSH-tunnel
setup (Approach 2: `ssh -R 18421:localhost:8421`, client sets
`VOXD_HOST=127.0.0.1 VOXD_PORT=18421`) makes a **remote** machine B appear to
the daemon as a loopback peer. Any design that grants extra capability to
"local" connections classified by peer address hands the arbitrary-write
primitive straight back to the tunnelled remote. Peer address is therefore
**not** an input to the security decision.

## Security invariant (the P1 — non-negotiable)

> The daemon never writes to, plays, or reads a client-dictated path. Every
> record write, every playback, and every retrieval resolves to a path
> **contained within a daemon-owned root**; a client supplies at most a **bare
> name** (no directory component) or a generated **id**, never a path.

Concretely, for every wire operation that names a file:

1. Reject if the token is absent/invalid (unchanged, `router.py`).
2. Reject a candidate that is absolute, contains a path separator (`/`, `\`),
   contains a `..` component, is empty, or contains a NUL — with an error frame,
   before any filesystem touch.
3. Resolve the candidate under the root: `resolved = (root / name).resolve()`.
4. **Verify containment**: require `resolved.is_relative_to(root.resolve())`;
   reject otherwise. The check runs *after* `.resolve()` so symlink and `..`
   normalization cannot smuggle an escape.
5. Write through the existing TOCTOU-safe primitive (`mkstemp` fd + `os.replace`
   inside the root, already in `RecordSink._copy`).

The root is daemon-owned and `0700`, only the daemon writes there, and names
cannot contain separators — so no attacker-planted symlink lives inside the root
to follow out of it. Containment is closed by construction, not by trusting the
peer.

## The crux — local-vs-remote coherence

Locally and remotely, `vox record` produces a recording *in the daemon store*
and reports how to reach it. Materializing a user-side copy is a **separate,
explicit step** (`vox fetch`), never a side effect of recording. This is the
**pure model** (operator-ratified 2026-07-21): the daemon owns the file; the
client is a controller that captures and references, never a path the daemon
writes for it. The security-bearing decision (where the daemon writes) and the
ergonomic decision (where a copy lands) are cleanly separated across two
commands, so the record path has *no* client-path branch at all.

### Chosen: (a-pure) uniform daemon-owned store; record captures, fetch materializes

`vox record` **always** writes to the store under a content-addressed name (or a
validated bare name) and prints a locator — never a client path, and it takes
**no `-o`**:

- **Local (shared filesystem):** the reported `store_path` exists on the user's
  own filesystem, so the locator names the store id and the on-disk store path.
  The user can `afplay`/`vox play` it directly; `vox fetch` copies it elsewhere
  if wanted.
- **Remote (store_path not visible):** the locator names the store id and the
  host (`recorded a1b2c3.mp3 on <host> — play: vox play a1b2c3.mp3; fetch: vox
  fetch a1b2c3.mp3 -o <path>`). No client absolute path is echoed.

To get the bytes onto the client machine, **`vox fetch <id> -o <path>`** is the
one materialization path — it **always retrieves the bytes from the connected
daemon** (`fetch` wire op) and writes them to `<path>`. There is no local-copy
shortcut: a same-named file under this machine's recordings dir is not provably
the connected daemon's recording (a remote daemon may share this user's home
layout), and the client cannot confirm identity without the daemon's bytes, so
it fetches them. The daemon returns only facts; the client formats.

This keeps `#351`'s win intact: the **hot** record path ships **no audio bytes**
(it returns a locator). Bytes reappear only on the **cold**, opt-in `fetch` — a
path that did not work before this change anyway.

### Rejected options

| Option | Rejected because |
|--------|------------------|
| **`record -o` as client-side delivery** (the original a) | The operator ruled the pure model: overloading `record` with a delivery path re-introduces a client path on the hot command and a local/remote branch the user must reason about. Separating capture (`record`) from materialization (`fetch`) keeps `record` a single, path-free verb and puts the one copy-vs-wire decision behind `fetch`. |
| **(b) local fast-path** — loopback keeps direct client-path writes; remote is sandboxed | **Unsound by the guide's own topology.** The SSH-tunnel setup makes a remote B a loopback peer, so "local = trusted" hands the arbitrary-write primitive back to the tunnelled remote. It also keeps the dangerous write path alive and makes peer classification a security-critical branch — complexity for a capability we are removing. |
| **(c) always stream bytes back; client writes locally** — removes the daemon write primitive entirely | Reverts `#351`: re-adds base64/bytes-over-wire on **every** record, re-introducing the 1 MiB frame ceiling, the 33% inflation, and the unbounded client receive buffer that `#351` eliminated by construction. It also abandons the daemon-audio-host model P2/P3 depend on — with no daemon store there is nothing coherent for `vox play <id>` to reference. |
| **(a′) daemon returns and echoes the client-requested absolute path** | Echoing a client path is the exploit surface itself; the whole point is the client never names a daemon path. |

## Mechanism

### Store layout and id scheme

- Root: **`~/.punt-labs/vox/recordings/`**, created `0700` by
  `paths.ensure_user_dirs` alongside `logs`/`run`/`cache`.
- Flat directory. Default name is the content-addressed
  `punt_vox.types.generate_filename(text)` (`md5(text)[:12].mp3`) — the same
  scheme every other vox MP3 uses, computed daemon-side from the text the daemon
  already holds. Identical text → identical file (idempotent, deduped).
- A client may supply an optional **bare** `name` (validated per the security
  invariant). The **id returned to the client is the store filename** — the
  stable reference for `play` and `fetch`.
- Retention is out of scope here (the store grows like the cache did before
  eviction). Follow-up: a `vox recordings clear` / size-bounded eviction reusing
  the cache policy. Noted, not blocking.

### Wire protocol (forward integration of the #351 contract — no shim)

**`record`** — the `#351` `output_dir`/`output_path` absolute-path fields are
**deleted**, not bridged. Request carries at most a bare `name`:

```json
{"type":"record","id":"<hex>","text":"<...>","name":"greeting.mp3",
 "...spec fields":"voice, provider, model, rate, language, ..."}
```

`name` is optional; absent → content-addressed. Replies are unchanged in shape
(immediate `recording` ack, then terminal `audio`) but the terminal frame
carries the store locator, never a client path:

```json
{"type":"recording","id":"<hex>"}
{"type":"audio","id":"<hex>","name":"a1b2c3.mp3","path":"<store_path>","bytes":12345,"cached":false}
```

`path` is the **daemon-owned store path** (safe to echo; it is not a client
path and grants no write capability). The client uses it only as the
shared-filesystem probe and the local-copy source.

**`play`** — new op. Plays a store file **on the daemon host** via the existing
serialized `PlaybackQueue` (flock-guarded; no audio is killed):

```json
{"type":"play","id":"<hex>","ref":"a1b2c3.mp3"}
{"type":"playing","id":"<hex>"}   // ack; playback runs detached, serialized
```

`ref` is a bare store name, resolved and containment-checked exactly like a
record name; a ref that is absolute, traversing, or outside the root is an error
frame. This closes P2: remote `vox play <id>` and the record→play loop both emit
audio on A. The flock serialization invariant is *strengthened* — all daemon
playback funnels through the one `PlaybackQueue`.

**`fetch`** — new op, opt-in retrieval for the remote case:

```json
{"type":"fetch","id":"<hex>","ref":"a1b2c3.mp3"}
{"type":"bytes","id":"<hex>","ref":"a1b2c3.mp3","data":"<base64>","bytes":12345}
```

`ref` is resolved and containment-checked identically. Returned in a **single
frame**, so it is subject to the same frame bound as the old base64 return.
Remote fetch of a recording above the frame limit is **not supported in this
cut** — the same limit that already made remote record >1 MiB non-functional
(`docs/vox-tnum-record-transport.md`), so **no working behaviour is lost**. A
chunked streaming `fetch` is a separate, formally-modelled follow-up if it ever
becomes a real requirement; keeping `fetch` single-frame keeps this design
linear and below the streaming-transport modeling bar.

### Client surface

- **`VoxClient.record`** drops `output_dir`/`output_path`; adds optional
  `name: str | None`. `RecordResult` becomes
  `(id: str, name: str, store_path: Path, byte_count: int, cached: bool)`.
- **`VoxClient.play(ref: str)`** → `play` op. **`VoxClient.fetch(ref: str) ->
  bytes`** → `fetch` op. Sync facade (`client_sync.py`) gains parity.
- **CLI `vox record`**: takes **no `-o`** and no output directory. It captures
  to the store and prints a locator — the store id/name plus, locally, the store
  path (usable directly); remotely, the host and the `play`/`fetch` commands.
- **CLI `vox play <ARG>`**: dispatch by a behavioural test — if `ARG` is an
  **existing file on the client filesystem**, keep today's client-side
  `play_audio` fast path (loopback = correct machine, no trust boundary
  crossed); otherwise treat `ARG` as a **store ref** and issue the daemon `play`
  op (plays on A). Precedence: an existing local file wins. Remote playback of
  an *arbitrary local file* (upload-then-play) is a documented follow-up (`push`
  op) — the coherent remote path today is record→`play <id>`.
- **New CLI `vox fetch <ref> -o <path>`**: the one materialization path — copy
  from the store path when locally visible, else `fetch` the bytes over the
  wire, then write `<path>` on the client.
- **MCP `record` tool** adapts to the new `RecordResult`; the result JSON
  reports `name`/`id` and the store path.

## Recommended implementation write set

The design mission's output *is* the write set; the implementation specialist
finalizes splits and extractions.

### Create

- `src/punt_vox/voxd/record_store.py` — the daemon-owned store: `root`,
  `resolve(name, text) -> Path` (validation + containment), `place(source,
  text, name, cached) -> RecordWrite` (atomic, in-root), `resolve_ref(ref) ->
  Path` for play/fetch. This is the one module that owns the containment
  invariant; unit-testable without a socket. Supersedes `record_sink.py`.
- `src/punt_vox/voxd/play_handler.py` — the `play` op: resolve ref in-root,
  enqueue on `PlaybackQueue`, ack `playing`.
- `src/punt_vox/voxd/fetch_handler.py` — the `fetch` op: resolve ref in-root,
  return single-frame bytes.
- Tests: `tests/voxd/test_record_store.py`, `tests/voxd/test_play_handler.py`,
  `tests/voxd/test_fetch_handler.py`.

### Modify

- `src/punt_vox/voxd/record_handler.py` — `_resolve_sink` becomes bare-name
  validation over `RecordStore`; terminal `audio` frame carries `name`/store
  `path`/`bytes`/`cached`.
- `src/punt_vox/voxd/handler_registry.py` — register `play` and `fetch`; wire
  the shared `RecordStore` and `PlaybackQueue`.
- `src/punt_vox/paths.py` — add `recordings_dir()`; create it `0700` in
  `ensure_user_dirs`.
- `src/punt_vox/client.py` / `client_sync.py` — new `record` signature and
  `RecordResult`; add `play`/`fetch`.
- `src/punt_vox/__main__.py` — `record` `-o` delivery; `play` dispatch; new
  `fetch` command.
- `src/punt_vox/server.py` — `record` tool adapts to the new result.
- `docs/guide-remote-setup.md` — rewrite the record/play section (below).
- `DESIGN.md` — ADR DES-049; `CHANGELOG.md` — Security + Changed entries.

### Delete (forward integration, no shim)

- `src/punt_vox/voxd/record_sink.py` and `tests/voxd/test_record_sink.py` —
  their responsibility moves into `record_store.py` with the containment check
  added. The `#351` absolute-path wire contract is superseded, not aliased.

## Test plan (cases assert the invariants by name)

Security / containment (the P1):

- **`test_wire_absolute_path_rejected`** — `record`/`play`/`fetch` with an
  absolute `name`/`ref` (`/etc/passwd`) → error frame; nothing written or read
  outside the root.
- **`test_wire_traversal_rejected`** — `name="../../../etc/cron.d/x"` → error;
  store unchanged.
- **`test_wire_separator_in_name_rejected`** — `name="a/b.mp3"` → error.
- **`test_write_cannot_escape_root`** — property test over a corpus of hostile
  names (absolute, `..` at every position, embedded NUL, symlink-shaped); the
  resolved path is always `is_relative_to(root)` or the op is rejected.
- **`test_token_does_not_grant_fs_write`** — the trust-model assertion: no
  authorized request, whatever its `name`, causes a write outside the root.
- **`test_play_ref_outside_root_rejected`** / **`test_fetch_ref_outside_root_rejected`**.

Coherence and ergonomics:

- **`test_default_name_is_content_addressed`** — no `name` → `generate_filename(text)` under the root.
- **`test_play_routes_through_daemon`** — `play` op resolves a store ref and
  enqueues on the daemon `PlaybackQueue` (assert enqueue called with the in-root
  path); playback is **not** client-side.
- **`test_record_returns_store_locator`** — `record` returns a `RecordResult`
  with an id/name and the daemon store path; no client-path field exists on the
  result.
- **`test_remote_record_returns_locator`** — store path absent on the client →
  the CLI prints a locator (id + host + `play`/`fetch` hints), not a bogus local
  path.
- **`test_remote_fetch_delivers_bytes`** — `fetch` returns bytes equal to the
  store file; `vox fetch -o` writes them byte-correct on the client.

## Live-verify plan (running machine — ask the operator after each audible step)

1. `make install`; `vox daemon restart` (the daemon serves code from startup).
2. **Local record→play loop.** `vox record "hello from vox"` → prints a store
   locator (id + store path). `vox play <id>` → audio plays on this machine.
3. **Local materialization.** `vox fetch <id> -o ./out.mp3` → `./out.mp3`
   exists, size matches; retrieved from the daemon (no local-copy shortcut).
4. **Hostile name rejected end-to-end.** `vox record --name '../../etc/x' "x"`,
   `vox play '../../etc/passwd'`, and `vox fetch '/etc/passwd' -o ./x` → each a
   one-line error, exit 1, nothing touched outside the root.
   `ls ~/.punt-labs/vox/recordings/` shows only in-root files.
5. **Remote (SSH tunnel per the guide).** From B: `vox record "remote hi"` →
   prints a locator naming the host; `vox play <id>` → audio plays on **A**;
   `vox fetch <id> -o ./remote.mp3` → the file appears on B, byte-correct.
6. **Arbitrary local file still plays locally.** `vox play ~/Music/song.mp3`
   (existing file) → plays on the local machine.

Log inspection alone cannot judge audio — confirm each audible step with the
operator.

## Guide rewrite (`docs/guide-remote-setup.md`)

Once this lands, the record/play section is rewritten to state:

- `vox record` against a remote daemon stores the recording on the daemon host
  and prints a **locator** (id/name + host), not a local path.
- `vox play <id>` plays on the **daemon host** (the machine with speakers) — the
  remote record→play loop now works.
- `vox fetch <id> -o <path>` retrieves a remote recording to the client.
- Remove the current caveat that `record`/`play` are loopback-only and that a
  daemon-side path is meaningless remotely — that limitation is closed.

## Out of scope (separate mechanical beads)

- **vox-4p5p** — daemon status via `client.health`. Its own quick fix.
- **vox-suvs** — cache via daemon. Its own quick fix.
- Store retention/eviction; chunked streaming `fetch` for large remote files;
  remote arbitrary-local-file playback (`push` op).

## Z-specification

The record-store containment property qualifies for formal modeling: it is a
security-critical **safety invariant** ("every stored write stays within the
root; no client-supplied name escapes the sandbox") whose violation is the
highest-stakes version of "expensive to discover late" — arbitrary file
overwrite. Modeled in **`docs/vox-dvri-record-store.tex`** (`fuzz -t` clean):
the store state carries the invariant in its schema predicate, `Place` proves it
is preserved, `Reject` covers the absolute/traversal/empty cases, and `Play`/
`Fetch` resolve only in-store refs. See that file for the schemas.
