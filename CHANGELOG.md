# Changelog

All notable changes to punt-vox will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **The Vox Control Panel — a session-scoped Lux applet for mic mode, notifications, and voice (vox-mhwj).** A `Vox` menu entry, launched per Claude Code session (mirroring lux's own `lux-beads` applet, spawned by a `hooks/session-start.sh` block gated on `.punt-labs/vox/enabled`) rather than hosted inside `voxd` like the Music Player. Opening it shows a Notifications radio (Off/Normal/Continuous, mirrors `mic:notify`), a Mic Mode radio (Chimes only/Voice, mirrors `mic:speak`), and a Voice combo populated from the roster with a `▶` preview button. Changes apply immediately through the same `notify`/`speak`/`voice` settings the CLI and MCP tool already write; a failed write or an unreachable `voxd` is shown in the panel itself (a transient status line), never only in a log. The new `vox-panel` console script and a new `src/punt_vox/panel/` package (`punt_lux.applets`-style session claim, Lux leg, and settings service) are `voxd`-independent — the daemon has no new wire method and no daemon-side lux client for this feature.
- **voxd music player — the transport controls (phase 3, vox-tqo1).** The `vox.music` scene gains a transport row — previous, play/pause, next, stop — that controls the now-playing album. Pause and resume suspend and continue playback **in place** via a persistent `mpv` process driven over its JSON IPC socket — `set_property pause` freezes the decoder gaplessly and click-free, so a paused source holds its part cursor and cannot auto-advance while paused. The program audio tier (music now; audiobooks and podcasts later) runs on this one persistent mpv, a **hard dependency** `install.sh` installs beside ffmpeg and `vox doctor` errors on when missing or older than 0.35; chime and speech playback keep the built-in `afplay`/`say`/`espeak` players. A missing or crashed mpv surfaces `PLAYER_UNAVAILABLE` on the program status (the daemon and notifications stay up), and the mpv process/connection lifecycle (spawn, crash, bounded restart, orphan-reap) is modelled in `docs/mpv-program-player.tex` (`fuzz`-clean, ProB-checked). Previous and next move the cursor within the album (previous at part 1 and next at the last part are no-ops), and moving the cursor while paused repositions playback without un-suspending it. Play from any state starts or **switches** to a clicked album, restarting at part 1 — no stop-first required. The play/pause button renders `⏸` while playing and `⏵` while paused, and the whole row is inert while nothing plays (playback starts from an album's own Play button). The daemon gains `pause`/`resume`/`prev` alongside the existing `next`, on both the `mic:music` and `vox music` surfaces. The idle/playing/paused state machine, its transitions, and its invariants are modelled in `docs/vox-music-player-transport.tex` (`fuzz`-clean, ProB-checked) before implementation.
- **The `vox.music` scene reads as a media player, not a debug dump.** The album table is sortable (click a column header), shows catalogue-unique, ID3-derived friendly names (title-cased, timestamp-stripped, collision-suffixed) instead of generation slugs, and renders directly rather than behind a collapsing header. The playing album is marked by **selecting its row** — lux highlights it — rather than a `▶` prefix in the name cell, which previously fought both the column sort and the click-to-play key. Now-playing shows the album name and its `N of M` position; the static progress bar and the per-track title line (which only ever carried the generation prompt, never a song name) are removed.
- **An album title authored at generation (vox-cdvk).** `music on` / `music new` now take a `title` — a human album name the authoring agent supplies alongside the prompt — which becomes the album's name and rides the ID3 `TALB`/`TIT2` frames, so a generated album carries a real title in any music player instead of a `{vibe}-{style}-{date}` slug. Absent a title, the timestamp slug is the fallback; `play` still selects a saved album by name.
- **A refused Play or Stop is shown in the scene, not just logged (vox-xvaw).** When a `music.play` names an album that has left the catalog or has no ready tracks, or a `music.stop` cannot apply, the `vox.music` scene carries a transient warning in a dedicated status line (`⚠ couldn't play <name> — no longer in the crate`) and clears it on the next successful action — the click no longer fails silently with only a daemon-log line.
- **`[lux]`-prefixed lifecycle logging across the music-player lux legs.** Every connect, menu registration, subscription, scene push, inbound click, disconnect, and reconnect writes one greppable `[lux]` line to the daemon log, so the whole receive/publish lifecycle replays with a single `grep '\[lux\]' ~/.punt-labs/vox/logs/vox.log`.
- **voxd music player — the interactive receive leg (phase 2, vox-efa6).** The `vox.music` scene is now clickable: in-scene Play/Stop buttons carry a `publish` attribute (`play-<id>` → `music.play {album_id}`, `stop` → `music.stop`), and `voxd` holds one persistent lux hub connection that registers a `Music` menu entry, subscribes to the two topics, and translates each inbound event into a single playback command — a play replays that album, a stop turns the source off — after which the phase-1 change signal re-pushes the scene. The daemon names itself with one explicit lux identity (`kind=app`, `name=voxd`, a 30s menu lease) for both legs instead of deriving it from a working directory (closes vox-hrjx). The receive leg honours the modelled `LuxListener` invariants (`docs/vox-music-player.tex`): at most one connection and subscription at a time, each event dispatched exactly once, and a boundary that logs and drops a malformed frame or a playback refusal so one bad event can never drop the connection. A guarded restart loop wraps the whole connect/register/subscribe/listen cycle and re-registers the `Music` menu on **every** fresh connection — first connect and reconnect alike — so once a >30s lease expiry lets luxd sweep the menu, the next connection restores it; the leg relies on no surviving Hub state. Any fault out of the listen loop — including a protocol frame that fails validation deep inside it — is logged to the daemon log and the cycle restarts after a backoff, so the receive leg can never die silently, and a fatal fault in either leg tears the other down cleanly (one `asyncio.TaskGroup`) instead of orphaning it. The end-to-end live click — a real click on the lux display driving voxd playback — is live on this release, verified against luxd's Hub-side publish handler.
- **voxd music player — the `vox.music` lux scene (phase 1, push-only, vox-efa6).** `voxd` now hosts a headless app that mirrors the saved-album catalog and the current now-playing onto a lux scene, keeping a live browse-your-music surface truthful as playback and the catalog change. The daemon holds lux's public `LuxRestClient`, builds the `vox.music` scene (a now-playing marquee, a Stop control, and one Play-buttoned row per album), and re-pushes it on every state change — a play or stop from any surface (CLI, `mic`, or a lux click), an auto-advance on track-end, or a catalog `music new`/`music remove`. The player adds no audio machinery: it is a projection of voxd's one active source, and the modelled `PlayerView` invariants (at most one album playing; now-playing present iff playing; a played album is catalogued) are pinned in `docs/vox-music-player.tex`. The re-push never blocks playback: the change signal fires inside the control-channel single-writer but only hands the freshly built scene to a latest-wins mailbox, and the blocking `PUT /scenes/vox.music` runs off-thread on the publisher's own task, where a slow or unreachable luxd is logged and dropped — a dead display can never stall audio. The in-scene Play/Stop buttons render but are inert until the phase-2 receive leg lands.

### Changed

- **The music halt verb is now `stop`, not `off`.** `vox music stop` and `mic:music stop` replace `vox music off` / `mic:music off`, aligning the transport vocabulary (play / pause / resume / prev / next / **stop**) with the lux "Stop" button. `on` is unchanged — it still starts the generative radio. There is no `off` alias: the old verb is gone on every surface (the CLI, the MCP `music` tool, and the daemon's `program_stop` wire method).
- **A bare `music play` replays the last-played album.** `vox music play` / `mic:music play` with no argument now repeats the album you last played instead of silently starting the first album in the catalog. The daemon records each single album it plays; a no-argument `play` replays that recorded album. When nothing has played yet it fails with `no album played yet; specify an album by id, name, or style/vibe` and prints the saved-album list, rather than starting an arbitrary album. `play <id>` / `play <name>` / `play --style/--vibe` are unchanged.
- **`punt-lux` floor raised to `>=0.23.0`** — require punt-lux >= 0.23.0 for album click-to-play, which ships the publish-event-payload passthrough (lux-r4pp) that delivers the clicked album row's key to voxd.
- **`punt-lux` pinned to the PyPI release `>=0.22.0`** (vox-hnfr), retiring the interim git pin now that lux ships the Hub-side publish handler and the reconnect port re-resolution the Music Player needs.
- **Enable/disable moved under `/vox` (`/vox enable` | `/vox disable`) to fix a slash-command namespace collision.** Enablement was exposed as top-level `/enable` and `/disable`, which claimed those generic verbs in Claude Code's shared slash-command namespace. They are now two more `$ARGUMENTS` subcommands of `/vox`, beside `/vox model` and `/vox provider`, calling the same `mic:enablement` tool with identical confirmation text. The old `commands/enable.md` / `commands/disable.md` are deleted (no shim), and `hooks/session-start.sh` retires them so an already-installed plugin drops the stale top-level commands on the next session start. The `vox enable` / `vox disable` CLI verbs are unchanged (DES-060).

### Fixed

- **`mic:music subcommand="status"` did not exist, though `/music status` and the deposited guide both told agents to call it (vox-9gqu).** FastMCP builds the tool's schema from the `MusicSubcommand` literal and validates the argument before dispatch, so the documented call was rejected outright and never reached a handler — `/music status` and a bare `/music` were broken on the MCP surface, while the `vox music status` CLI worked. The verb now exists and reports the daemon's authoritative `program` block, the coarse `music_mode` label, and a one-line summary: the same music state the `status` tool reports, from one shared `MusicStateView` projection, so the two surfaces cannot drift. `hooks/suppress-output.sh` treats it as the query verb it is — the program state reaches the agent, instead of the stop-narration directive its `*` catch-all applies to fire-and-forget control actions.
- **A voice the config could not save reverted in the Vox Control Panel with nothing on screen saying why.** `Frontmatter.validate_value` rejects a value carrying a newline or a double-quote — a real risk for a custom or cloned ElevenLabs voice whose display name contains one — but it raised a bare `ValueError`, indistinguishable from the `ValueError` a malformed wire payload raises. The panel routed it to the log-only bucket, so the combo snapped back with no explanation, while the functionally identical disk-write failure correctly showed `⚠ couldn't save voice -- reverted to the last saved value`. Frontmatter now raises a dedicated `ConfigValueError` (a `ValueError` subclass, so existing handlers are unaffected), and the panel answers it the way it answers a failed write: re-sync from the real settings and show the notice.
- **Two-tier audio levels rebalanced so speech and chimes sit above music, not under it.** Voice playback ran at the `afplay`/`ffplay` default (100%) while the mpv music program ran at 30% — a roughly 3x gap that left narration hard to hear over the background track. The notification/speech tier now plays at 80% (`afplay -v 0.8`, `ffplay -volume 80`) and the music program at 60% (mpv `--volume=60`), an interim static balance confirmed workable by ear. User-tunable levels and dynamic ducking (drop the music while speech plays, then restore it) are the follow-up, tracked in vox-wy7u.
- **A stale `end-file` could skip the freshly-loaded track after a boundary next/prev.** The mpv player's ended-future was armed before `loadfile` was acknowledged, so an in-flight `end-file` `eof` from the still-loaded previous part could resolve the *new* track's future while the load was in progress — the loop then treated the just-loaded part as already finished and advanced past it. The future is now guarded so an `eof` for the prior load cannot resolve the new one.
- **A `next`/`prev` at the pool boundary restarted the current track instead of no-op'ing.** A boundary step is modelled as a no-op, but the step signal still declared an interrupt, so the control channel reloaded the current part from the start. A boundary no-op no longer fires the interrupt; playback is left untouched.

### Security

- **`cryptography` bumped `49.0.0` → `50.0.0` (GHSA-g6cj-pr64-35w5 / CVE-2026-69247, high severity, vox-2q3i).** A transitive dependency (`punt-lux` → `fastmcp` → `authlib`/`joserfc`, and `mcp[crypto]` → `pyjwt[crypto]`), not imported directly by vox. The vulnerable range (`>=44.0.0,<50.0.0`) exposed a Bleichenbacher oracle in PKCS#7 `EnvelopedData` decryption through distinguishable errors and timing. No source change required — every parent dependency already accepted the newer version; the resolved lock is the whole fix.
- **The persistent mpv is spawned with `--ytdl=no` alongside `--no-config`**, so a crafted media path cannot trigger network or script fetching through mpv's youtube-dl integration.
- **The now-playing album title is markdown-escaped** before it reaches the scene's heading element — a curated album name is trimmed but was not previously escaped for markdown/HTML metacharacters.
- **`MpvProgramPlayer.play` validates the resolved part path is a regular file contained in its album directory before `loadfile`**, matching the containment check the `music get`/fetch path already applies — a hostile on-disk manifest `file` field can no longer make `loadfile` open a path outside the album directory.
- **`OrphanReaper.reap()` treats a connect `EACCES` as a bring-up hard stop, not just a no-unlink.** An unprobeable socket previously left the path in place but still let the supervisor spawn a second mpv, breaking the single-mpv invariant; it now aborts the bring-up and folds into the existing retry-to-`failed` path.

## [4.16.0] - 2026-07-28

### Added

- **`vox enable` / `vox disable` — per-repo enablement (vox-ck3w, `tool-enable-disable.md` conformance).** vox now turns on per repo via a committed `.punt-labs/vox/enabled` marker. `vox enable`, run in a repo, deposits the `.punt-labs/vox/CLAUDE.md` agent guide, writes the marker, adds the `@.punt-labs/vox/CLAUDE.md` import to the repo's `CLAUDE.md`, additively registers the enable/disable skill permissions, and establishes an audible notify level (`normal`) so a freshly-enabled repo chimes and speaks on task completion — silence is `vox disable` (marker removed, hook gate closed), never an enabled repo left at `notify=n`; a re-enable preserves an existing `continuous` choice. `vox disable` reverses the import and marker, leaving the guide dormant (non-destructive); `vox disable --purge` removes the subtree. The same verbs are on the `mic` MCP surface (`mic:enablement` with `action="enable"|"disable"`) and as `/enable` / `/disable` slash commands — all three write a byte-identical marker. No auto-enable: vox never turns itself on as a side effect of use.
- **`install.sh --no-plugin` / `VOX_NO_PLUGIN=1` — CLI-only install (`install-cli-only.md` conformance).** Installs the `vox` CLI and its user-scope guidance import without the Claude Code plugin — for scripts, CI, and non-Claude harnesses. Both `curl … | sh -s -- --no-plugin` and `curl … | VOX_NO_PLUGIN=1 sh` work, and the plugin is auto-skipped when `claude` is absent. A `--no-plugin` agent drives vox through the CLI, guided by the deposited guide's unconditional CLI section.

### Changed

- **vox's audio session hooks gate on the per-repo marker.** The Stop, Notification, and PostToolUse chime/narration hooks now fire only in repos that ran `vox enable` — vox is silent in a project until it opts in, and a clone on a box without vox is a graceful no-op. This replaces the retired `vox.md`-presence check.
- **The deposited `.punt-labs/vox/CLAUDE.md` guide is surface-aware** — it routes an agent to the `mic` MCP tools when present, otherwise to the `vox` CLI, so a plugin-less agent can still drive vox.

### Removed

- **`/vox y|n|c` is retired** (`tool-enable-disable.md` §2.3 — `y|n` is not an enablement vocabulary). Per-repo on/off is now `/vox enable` / `/vox disable`; the notify level moves to `vox notify normal|continuous`. The legacy managed-section `CLAUDE.md` marker code (`managed_section.py`, `markdown_fence.py`) is removed in favor of the bare `@`-import writer.

### Security

- **`vox enable` refuses to follow a symlink at its tool-owned paths.** The `.punt-labs/vox/enabled` marker and the deposited `.punt-labs/vox/CLAUDE.md` guide are now written with `O_NOFOLLOW`, so an untrusted repo that plants either path as a symlink to a sensitive file (`~/.ssh/id_rsa`, credentials) can no longer redirect the write onto the link's target. Such a path is refused with a clear error across all three surfaces (CLI, `mic`, slash command); the target is never overwritten. The host `CLAUDE.md` and `.claude/settings.json` writes remain intentionally symlink-resolving (dotfile managers symlink those).

## [4.15.0] - 2026-07-25

### Added

- **`vox music on` — CLI pool authoring from stdin (Audio Programs Phase 1.5).** The `vox music on` verb returns — 4.14.0 removed it — now as the structured-input authoring path: it reads a 12-variation pool as JSON on stdin (`cat pool.json | vox music on --style <genre>`, where `pool.json` is `{"base_prompt": "...", "variations": [... 12 ...]}`) and starts a generating program from it. With no piped input (a tty or empty stdin) it falls back to the daemon's minimal prompt. A malformed pool — bad JSON, the wrong variation count, or a non-object payload — is a clean CLI error, not a traceback.

### Changed

- **One MCP tool per audio group (Audio Programs Phase 1.5).** The `mic` surface collapses the twelve separate `music_*` and `rec_*` tools into one `music` tool and one `rec` tool, each taking a `subcommand` argument — `mic:music` with `subcommand` in `{on, off, play, next, new, list, get, remove}` and `mic:rec` with `subcommand` in `{new, list, play, get, remove}`. The mapping is uniform (`vox <group> <subcommand>` corresponds to `mic:<group> subcommand=<subcommand>`), so the CLI and MCP are two views of one structure. Both surfaces build one `PromptSet` and send it to the daemon; the `music new` wire key is `base_prompt`.

### Fixed

- **`--json` in the trailing position on the `music` and `rec` subcommands (vox-cnak).** `vox music list --json` and `vox rec list --json` were rejected because the sub-app commands did not declare the flag; they now accept `--json`/`--verbose`/`--quiet` in every position, matching the top-level commands.

## [4.14.0] - 2026-07-25

### Added

- **One verb vocabulary for both audio stores (vox-jei3).** `vox rec` and `vox music` are now parallel command groups sharing `new` / `list` / `play <id>` / `get <id>` / `remove <id>` (music keeps `next` / `status`). `vox rec new "text"` synthesizes into the daemon-owned recordings store and prints the bare id; `vox rec list` enumerates it; `vox rec get <id>` writes the recording into the current directory under its store name; `vox rec remove <id>` deletes it. `vox music new "<prompt>"` generates one track from a verbatim ElevenLabs prompt into a fresh single-track catalog album (it does not disturb the running program); `vox music get <id>` / `vox music remove <id>` export/delete an album by its catalog id. The same verbs are exposed on the MCP `mic` surface at parity (`mic:rec_new`/`rec_list`/`rec_play`/`rec_get`/`rec_remove`, `mic:music_new`/`music_get`/`music_remove`), each routing through the one engine.
- **Chunked `get` transport — any size.** Fetching a recording or album part streams the bytes in bounded, ordered, sha256-verified chunks and lands atomically, so a long recording or a real multi-MB music track transfers in full.

### Changed

- **`vox music play` accepts a bare `<id>` positional** in addition to the shipped `--style`/`--vibe`/`--name` tag selection (both resolve).
- **`get` never names an output path** — it writes into the current directory under the store's own name, refusing to clobber an existing target.

### Removed

- **The scattered top-level `vox record`, `vox play`, and `vox fetch` commands, the `-o`/`--output` flag on the old `fetch`, and `vox play <localfile>`** — superseded by the `vox rec` group (forward integration, no aliases). Local files are played with the OS tool (`afplay`/`ffplay`); `vox say` stays the ephemeral speak-now verb. The 700 000-byte single-frame fetch ceiling (`FETCH_FRAME_LIMIT_BYTES`) is gone with the chunked transport.
- **`vox music on`** — start the background-music radio with `vox music play --style <genre>` (or `--vibe`) instead; `vox music off` still stops it.

## [4.13.1] - 2026-07-22

### Security

- **Blocked record/play/fetch operations now leave an audit trail in `vox.log` (vox-1hbo)**: the vox-zu39 containment correctly *refused* a hostile record `--name` or a play/fetch `ref` (absolute, traversing `..`, separated, empty) and an unknown recording, but the rejection was silent — a `grep` of `vox.log` for a blocked path-escape probe returned nothing, so an operator could not detect probing of the recordings root while successes and resource failures were both logged. Every rejected daemon store op (a containment `ValueError` **and** a not-found probe) now emits one `WARNING` to the single `vox.log`, carrying the wire request id and the reason, so a blocked attempt is greppable alongside everything else. The logging is centralized: a new `WireReply` value object (bound to one request's socket and id) is the one place record, play, and fetch emit an error frame, and it logs the rejection there — the duplicated per-handler `_error`/`_safe_reply` helpers are removed. The attacker-controlled name/ref is **sanitized before it reaches the log** — newlines and control characters are escaped and the field is length-capped — to close a log-injection vector into `vox.log`; the client-facing error frame carries the message verbatim, so the wire protocol and exit behavior are unchanged. The store validator (`RecordStore`, shared by record/play/fetch) additionally now **rejects any control or non-printable character in a name** (`not str.isprintable()`), not just NUL — a crafted `--name` with an embedded newline or terminal escape is refused before any filesystem touch, closing a terminal/log-injection path through the record locator that would otherwise print the raw name. The rejection is delivered and audit-logged like any other; an oversize `fetch` (a legitimate too-large recording, not a probe) is logged at `INFO` for a symmetric audit trail. Closes vox-1hbo.

## [4.13.0] - 2026-07-22

### Added

- **`vox fetch <id> -o <path>` materializes a stored recording (vox-dvri, vox-eoq9)**: always retrieves the recording's bytes from the connected daemon (a single-frame wire transfer) and writes them to a local path. There is no local-copy shortcut — a same-named file under this machine's recordings dir is not provably the connected daemon's recording, so the client fetches the bytes; a recording above the single-frame limit is refused (retrieve it from the daemon host directly). The reference is a bare store id/name; no daemon path is named.

### Fixed

- **`vox record` no longer breaks past ~1 MiB — no audio crosses the wire (vox-tnum)**: a long recording (a ~6000-char text = a ~3 MB MP3) died with `ConnectionClosedError: sent 1009 (message too big)` because the daemon returned the audio as a single WebSocket frame over the client's 1 MiB `max_size`; a fresh long synthesis also blew a fixed 30 s client timeout, and both the client-side `ConnectionClosedError` and a daemon-side `AssertionError` on the interrupted send surfaced as raw tracebacks. The transport is deleted, not tuned: **`voxd` now writes the synthesized MP3 to disk itself (atomically, via a temp file + `os.replace`)** — no audio crosses the wire, so there is no frame ceiling and no receive buffer to bound. The client waits for the terminal reply under a length-scaled deadline (`60 + 0.05·len`, capped at 600 s) after an immediate `recording` ack, so a legitimate multi-minute synthesis is not abandoned. Every transport failure (`ConnectionClosed`/`WebSocketException`/`OSError`) is wrapped into a one-line CLI error instead of a traceback, and the daemon's reply is guarded so a client vanishing mid-transfer can neither crash it nor corrupt its send state. The old byte-return path and `recording.py` (`RecordingSink`) are removed (forward integration, no shim). **Superseded within this release by vox-dvri** (see Changed / Security): `#351`'s client-chosen `output_dir`/`output_path` and the `{path, bytes}` return are replaced by the daemon-owned recordings store and a locator (`RecordResult`) — `record` no longer takes `-o`, and remote record is now coherent (`vox play`/`vox fetch` by store id) rather than out of scope. Design of record: `docs/vox-tnum-record-transport.md` → `docs/vox-dvri-daemon-audio-host.md`. Closes vox-tnum.
- **"One `vox.log`" now actually holds every record — the ship transport and `vox-fallback.log` are gone (vox-2594)**: v4.12.5's fdmm logging rebuild promised a single log but shipped two — a hook that did no daemon work (the `Stop`/`UserPromptSubmit`/`SubagentStop` skip paths, config updates) never opened a WebSocket, so its records drained into `vox-fallback.log` even with the daemon up, and those hooks are the majority of fires (live measurement: 4.2 MB fallback + rotations vs a 404 KB `vox.log`). The transport is deleted rather than patched: **every process — daemon and every client — now appends directly to one `vox.log`** through the multi-writer-safe `O_APPEND` line writer (`AtomicAppendLog`), so a record reaches the file by local syscalls with no socket and no daemon dependency (DES-017 preserved: a hook's logging hot path never waits on a round-trip). The four ship modules (`log_ship.py`, `log_flush.py`, `log_wire.py`, `voxd/log_sink.py`) and the fallback file are removed outright — no shim, no migration. The one new safety mechanism is a `flock` shared/exclusive rotation protocol on a stable lock file (shared lock held across every append; exclusive lock + size re-check to rotate), formally modeled in `docs/vox-2594-log-rotation.tex` (fuzz-clean) and tested by invariant name: no lost or torn line under concurrent writers, no write to a renamed file, at most one rotator, `0600` on the active file and every backup. Stale pre-fdmm runtime files (`tts.log*`, `voxd.log*`, `hook-errors.log`, `voxd-stderr.log`) are confirmed unwritten and cleaned up. Design of record: `docs/vox-2594-unified-log.md` (DES-048). Closes vox-2594.

### Changed

- **The daemon is the audio host: `vox record` captures to a daemon-owned store, `vox play` plays on the daemon host (vox-dvri, vox-ovb7, vox-eoq9)**: `vox record` no longer takes `-o` and never names a daemon path. It synthesizes into a daemon-owned recordings store (`~/.punt-labs/vox/recordings`, `0700`) under a content-addressed name (or an optional bare `--name`) and prints a **locator** — the store id plus, locally, the on-disk store path; remotely, the host with the `vox play`/`vox fetch` commands. `RecordResult` becomes `(id, name, store_path, byte_count, cached)` — a locator, not a client write path. **`vox play` now routes through the daemon** (a `play` wire op onto the serialized `PlaybackQueue`), so a recording plays on the machine with speakers even when driven from a remote client; an argument that is an existing local file still plays client-side (loopback = the right machine). New `play(ref)`/`fetch(ref)` methods on `VoxClient`/`VoxClientSync`, and the MCP `record` tool drops `output_path`/`output_dir` for a bare `name` and reports the store locator. The `#351` `output_dir`/`output_path` absolute-path wire contract is superseded, not bridged (forward integration, no shim). Design: `docs/vox-dvri-daemon-audio-host.md`; containment model `docs/vox-dvri-record-store.tex` (fuzz-clean); ADR DES-049.
- **prfaq + README repositioned — vox is the voice and audio layer, not a notification tool, with three co-equal surfaces (vox-iyqq)**: the PR/FAQ (v1.3 → v2.2) and the README lede now lead with vox as the voice and audio layer for AI coding agents — voice personalities, mood-aware `/vibe` delivery, background music — of which spoken notifications are **one use, not the whole product**. The eyes-free benefit is stated only to what the evidence supports: listening and watching the screen draw on largely separate cognitive channels (Baddeley working-memory dissociation + HCI audio-notification sources) — with **no** "reduces cognitive load" claim, no percentage, and no misapplied modality-effect or dual-coding citation, plus an explicit boundary disclaimer in the evidence FAQ. The **MCP server**, the **Python library** (`VoxClient`/`VoxClientSync`), and the **`vox` CLI** now get co-equal, first-class treatment with a concrete example each, so vox reads as usable outside a Claude Code plugin. **Fun is a feature (DES-047)**: entertainment is now in scope — the old "agent personality voices / not personality entertainment" Won't-Do exclusion is **deleted**; the agent-as-DJ is called out (shipped) and codebase-aware podcast/audiobook programs are on the roadmap. The work/notification voice's mood stays honest signal, not a performed persona (DES-042 unchanged). The competitors FAQ is rewritten as a factual **AgentVibes head-to-head**: it concedes voice-quality parity (AgentVibes shipped ElevenLabs in v5.11.0, 2026-06-27, so premium voice is no longer a vox differentiator) and rests vox's edge on its reusable importable engine (`VoxClient`, used by langlearn-tts), dynamic auto-`/vibe` mood (vs 19 static personalities), generated vibe-matched music (vs a bundled MP3 catalog), and lean Python footprint (vs ~65% JavaScript + a `blessed` TUI) — with an honest note on what vox can learn from AgentVibes (offline voice breadth via local neural models). Closes vox-iyqq.

### Security

- **`vox record` can no longer overwrite an arbitrary file as the daemon user — the daemon owns the recordings store and every path stays inside it (vox-zu39)**: `#351` moved recording writes daemon-side but let a wire client dictate the absolute `output_dir`/`output_path`; `RecordHandler` checked only `is_absolute` and `RecordSink` `mkdir`-ed the parent and `os.replace`-d onto it. Across the documented remote setup (machine B holds A's `VOXD_TOKEN`, `docs/guide-remote-setup.md`) a compromised B could make A's daemon overwrite any file A's user can write — `~/.zshenv`, a launchd plist, an ssh key — i.e. likely RCE. The token authorizes audio operations, not filesystem writes as the daemon user. The fix removes the primitive by construction: a client now supplies at most a **bare name** (or nothing → content-addressed), and a new `RecordStore` resolves it under a daemon-owned `0700` root, **rejecting anything absolute, separated, traversing (`..`), empty, or NUL-bearing before any filesystem touch** and verifying `resolved.is_relative_to(root)` *after* `.resolve()` (so a symlink or `..` cannot smuggle an escape) before the existing atomic `mkstemp`-fd write. The same containment guards the new `play` and `fetch` wire ops. The trust boundary is **not** the network interface — a loopback peer can be a tunnelled remote — so peer address is never a security input. The containment invariant is formally modeled in `docs/vox-dvri-record-store.tex` (`fuzz -t` clean) and tested by name (`test_wire_absolute_path_rejected`, `test_wire_traversal_rejected`, `test_write_cannot_escape_root`, `test_token_does_not_grant_fs_write`). Closes vox-zu39.

## [4.12.5] - 2026-07-19

### Changed

- **Logging consolidated onto a single `vox.log`, with per-decision observability and a humble `log_level` toggle (vox-fdmm)**: the daemon (`voxd`) becomes the *single writer* of one durable log. Every client process — the `mic` MCP server, each hook, the CLI, detached playback — ships its log records to `voxd` over the WebSocket it already opens (`log_ship.py` → `voxd/log_sink.py`), which re-emits them into `vox.log`; the multi-writer `tts.log` and its rotation race are deleted, and the two duplicated `dictConfig` blocks collapse into a single owner (`logging_config.py`: `configure_daemon_logging` + `configure_client_logging`). When `voxd` is unreachable — a daemon-down hook, a failed send, or a client that never connects — records fall back to a local `vox-fallback.log` via a reusable multi-writer-safe `O_APPEND` sink (`append_log.py`, which also now backs `vibe-trace.log`). The long-lived MCP server drains its buffer every ~2 s so records are durable within seconds, not only on the next tool call (`log_flush.py`). **`voxd.log` is renamed to `vox.log`** (it now carries both daemon and client lines). **No-silent-gaps observability**: Program mode transitions log once on change at the control-channel choke point, generation success is logged symmetrically with failure, provider auto-detection logs a deduplicated decision line (with the AWS probe recording *why* Polly was skipped at DEBUG), and a malformed hook payload logs a byte-count-only WARNING. **Noise reduction**: the seven-lines-per-chime collapse to one `played chime: done`; synthesis cache/direct-play lines become plain sentences (`synthesized 5 chars with openai, cached`); the daemon env dump, per-field config writes, transport connect/disconnect, and the config-absent hook line all drop to DEBUG; the `mcp` framework request logger is suppressed and replaced by a vox-owned `mic:<tool>` line per invocation. **`log_level`** is a new ephemeral config key (default `info`); `vox log debug|info` (and the level shown by `vox status` and `mic:status`) raises verbosity when debugging — client processes apply it immediately, the daemon on `vox daemon restart`. A daemon-startup crash during logging construction is captured to a dedicated `vox-boot.log` (`voxd/crash_logging.py::install_bootstrap_excepthook`). The `hook-errors.log` shell redirect is removed (hook warnings now ship to `vox.log`). Closes vox-fdmm.
- **Slash-command voice arguments drop the `@` sigil (vox-qefg)**: the documented form is now bare — `/unmute matilda` and `/unmute` (no argument) to browse — instead of `/unmute @matilda` / `/unmute @`. Typing `@` in the Claude Code prompt triggers its @-mention / file-path autocomplete, which mangled the argument before vox ever saw it. Help text (`commands/unmute.md`, `commands/vox.md`, `README.md`, the deployed agent guidance) now shows the bare form. Parsing is lenient: a stray leading `@` is stripped so an old-habit `/unmute @matilda` or `vox voice @matilda` still resolves to `matilda`, and a lone `@` is treated as no argument. `SynthesisSpec` normalizes its `voice` field once, so every synthesis path (CLI `vox say`/`record --voice`, the `mic:unmute`/`mic:speak` MCP tools) inherits the leniency from a single choke point.

### Security

- **`voxd`'s `/ws` now rejects browser-origin handshakes as CSWSH / DNS-rebinding defense**: `WebSocketRouter.handle_connection` refuses any handshake carrying an `Origin` header (close 1008, metadata-only WARNING) before inspecting the auth token. Browsers always send `Origin` on a WebSocket handshake; vox's native Python clients (CLI, hooks, MCP server, remote over `VOXD_HOST`/SSH tunnel) never do — so native and remote clients are unaffected while a scripted cross-site connection from a browser is turned away.
- **`mcp` SDK bumped 1.27.1 → 1.28.1, resolving three High Dependabot alerts (vox-uujq)**: the direct `mcp` dependency (`pyproject.toml`) is repinned to `>=1.28.1` and `uv.lock` is re-resolved (`fastmcp` stays at 3.2.0 — its own `mcp` requirement was already satisfied by the new floor). Exposure verified against vox's actual runtime paths, not assumed from the advisory text: (1) **#35** (GHSA-hvrp-rf83-w775, experimental task handlers letting one client read/cancel another's tasks, fixed in 1.27.2) — **not exposed**; nothing in the tree calls `server.experimental.enable_tasks()` (`grep -r enable_tasks` is empty). (2) **#37** (GHSA-vj7q-gjh5-988w, `mcp.server.websocket.websocket_server` accepting WS handshakes with no Host/Origin check, fixed in 1.28.1) — **not exposed**; `voxd`'s `/ws` endpoint (`src/punt_vox/voxd/daemon.py`, `src/punt_vox/voxd/router.py`) is a Starlette `WebSocketRoute` on its own ASGI app, never the SDK's deprecated websocket transport. (3) **#36** (GHSA-jpw9-pfvf-9f58, SSE/Streamable-HTTP session lookup by ID alone, no principal check, fixed in 1.27.2) — **not exposed**; the mic MCP server (`src/punt_vox/server.py`) runs `mcp.run(transport="stdio")` only — no SSE or Streamable HTTP transport is wired up anywhere. The bump is still required: Dependabot flags the dependency regardless of whether the vulnerable code path is reachable, and the pin floor now excludes the vulnerable releases outright. Closes vox-uujq.
- **Log files are created private atomically, re-tightened at startup, and never fail silently (vox-cn0p)**: three hardening changes to `PrivateRotatingFileHandler` close the gaps left after the initial `0600` work landed. (1) **New log files are created `0600` atomically** — `_open()` pre-creates the file with `os.open(..., O_CREAT | O_NOFOLLOW, 0o600)`, so a brand-new log carries owner-only permissions the instant it exists, closing the sub-millisecond window a plain `open()` + `chmod` leaves at the umask default. `O_NOFOLLOW` additionally refuses a symlink at the log path — never legitimate, and a redirect-through-symlink vector — surfacing as a loud configuration error rather than writing through the link. (2) **The active file and every backup slot are re-tightened to `0600` on handler construction, not only on rollover** — a legacy `0644` backup left by an earlier, laxer run that never rotates is now fixed the first time the handler runs (via the `from_config` startup factory that both `tts.log` and `voxd.log` use), instead of staying world/group-readable until it happens to rotate. (3) **A file that cannot be tightened is surfaced, not swallowed** — a `chmod` that genuinely fails (changed ownership, a root-run leftover) is collected and reported as a durable `WARNING` in the now-live log naming the still-loose path, so an un-securable log leaves a greppable trace rather than silently remaining readable. Tightening remains fail-open, so a transient permission error never crashes logging. Closes vox-cn0p.

## [4.12.4] - 2026-07-18

### Security

- **The logging surface no longer leaks secrets, forges records, or duplicates sensitive logs (vox-q637)**: a security pass over how vox writes logs closes seven distinct exposures. (1) **Log files and every rotated backup are forced to `0600`** — a new `PrivateRotatingFileHandler` (`log_handlers.py`) opens **both the client `tts.log` (`logging_config.py`) and the daemon `voxd.log`** owner-only and, on every rollover, overrides `doRollover` to chmod the active file **and every** `.N` backup to `0600`. This is stronger than tightening on `rotate`: the stdlib only routes the `base → .1` shift through `rotate` and shifts the outer `.N → .N+1` backups with a bare `os.rename` that preserves the source's mode — so a backup left `0644` by an earlier, laxer run would keep those bits as it ages outward. Overriding `doRollover` tightens the whole chain, so a session's transcript of provider names, voices, and request IDs is never world-readable on a shared host. (2) **Auth rejections are logged at the daemon's one authentication boundary**, recording only metadata (remote address, outcome) — never the presented token or key. (3) **The ElevenLabs music prompt is no longer logged verbatim at INFO**: a generated-music prompt (potentially long, user/agent-authored content) now logs its length and a short hash at INFO, with the full text only at DEBUG. (4) **Untrusted interpolated values can no longer forge or corrupt a log line**: a shared `LogSanitizer` (`log_sanitize.py`) maps every C0 control, DEL, and the full C1 range U+0080–U+009F — which includes CSI (U+009B), the start of an ANSI escape sequence and a terminal-corruption vector on `cat` — plus the Unicode line separators U+2028/2029 to visible escapes, applied to the free-form external sink (a player subprocess's stderr) so an embedded newline cannot inject a second record and a raw control byte cannot corrupt a terminal; the wire-scalar sinks (request ID, provider, voice, chime signal) log via `%r`, which neutralizes the same code points. (5) **The daemon logs exactly once, to the `0600` `voxd.log`**: the parallel stderr `StreamHandler` — whose output launchd captured into a world-readable `voxd-stderr.log` and systemd into the journal — is removed, along with the launchd `StandardOutPath`/`StandardErrorPath` keys, so no second, unprotected copy of the records exists. (6) **`vibe`/`vibe_tags` are logged by length, not content**: the config writer logged every set value verbatim at INFO, persisting the expressive, agent-authored mood text in the durable config log; those two keys now log a `<N chars>` summary while all other keys still log their value for operability. (7) **An uncaught daemon exception can no longer vanish silently**: removing the stderr handler in (5) left a crash with nowhere to surface — launchd no longer tees stderr and the journal receives nothing — so a new `CrashLogger` (`crash_logging.py`) installs a `sys.excepthook` (sync/startup path) and an asyncio loop exception handler (fire-and-forget tasks) at daemon startup, both routing an uncaught exception with its full traceback to the `0600` `voxd.log`, so a fatal error is recorded rather than lost. Closes vox-q637.

## [4.12.3] - 2026-07-17

### Fixed

- **`[vibe-trace]` observability now writes to a durable log file (vox-9po7)**: the vibe→music and auto-vibe proof trace previously went to the mic-server/hook stderr, which the Claude Code host discards — so `grep '[vibe-trace]'` found nothing and the DES-046 proof was unreachable at runtime. Traces now append to a persistent, greppable file at `~/.punt-labs/vox/logs/vibe-trace.log` (multi-process-safe atomic appends); the stderr path is removed. The trace sink's health is queryable through `mic:status`. Proof recipe: `commands/vibe.md`.

## [4.12.2] - 2026-07-16

### Changed

- **A mood change now re-pools the music while it's playing (vox-1jke → vox-q1z4)**: setting the session vibe — via `/vibe <mood>` or the agent's own auto-vibe assessment — now switches the background music to match the mood, *when music is already playing*. The current track finishes, then playback switches to the `(new_mood, style)` pool: an existing pool rotates in for free, a new one is generated from rich, mood-colored genre prompts (e.g. `flamenco` + `relaxing` → slow soleá/guajira; `flamenco` + `intense` → fast bulerías). When music is **off**, a vibe change updates the speaking mood only — a music no-op. There is no confirmation and no credit prompt; the re-pool is the intended effect. Design: **`vibe()` stays a pure voice-mood tool** — it does not drive playback. It reads music status read-only and returns an imperative `music_hint` directive that steers the agent to author the prompts and call the existing `music` tool, which performs the re-pool (clean layering — vibe = mood, music = music, agent = orchestration). The hint fires only on genuinely-audible playback (never on a failed/retrying pool), and the playing style is tracked so the hint always names the genre actually playing (correct across `music_play` and `music off`). Because the mechanism is soft (it relies on the agent following the hint), a stable `[vibe-trace]` event log at each link (nudge → vibe-set → re-pool) makes it *provable* — `grep '[vibe-trace]'` shows whether the chain fired, and equally whether auto-vibe is working. Design: DES-045, DES-046. Closes vox-q1z4.

## [4.12.1] - 2026-07-15

### Changed

- **The ♪ music panel speaks like a DJ (vox-1jke)**: the `music` control panel lines are now DJ-booth flavored and varied — `/music on` reads `♪ dropping a trance beat` / `♪ trance in the booth` / `♪ cueing up trance`, `/music off` reads `♪ fading out` / `♪ that's a wrap` / `♪ decks off`, a replay reads `♪ now spinning: <name>` / `♪ <name> — encore`, a skip reads `♪ mixing the next one in`. Previously these lines were dead: the hook's phrase pools keyed off `.status`/`.style`/`.name` fields the music tools never return, so the panel silently showed a generic line (fixed to echo the plain server message in vox-lf6b). Now the **server** authors the flavored line — a new `MusicMarquee` (`music_phrases.py`) selects a randomized phrase from a curated per-action pool and interpolates the real style/name — and the `suppress-output.sh` hook stays a dumb `.message` echo (dead pools deleted). The panel uses the style/name, never the session mood (per vox-5aom). Design: DES-044. Closes vox-1jke.
- **Music and vibe control actions produce no agent narration — the audio panel is the whole response (vox-lf6b)**: `/music` (on/off/next/play) and `/vibe` are meant to be confirmed by the ♪ audio panel alone, but nothing enforced the "no text" contract — only a soft line in `commands/music.md` that the agent had to read and remember, and which failed in practice (the agent narrated "Trance pool is generating…" after `/music on`). The `suppress-output.sh` PostToolUse hook — which fires on every `mic` tool call — now enforces it: on a **success** of the fire-and-forget tools whose every slash-command flow wants silence (`music`, `music_play`, `music_next`, `vibe`) it injects a terminal stop-narration directive into the model's `additionalContext` channel instead of the raw result JSON that was inviting narration, so the silence is automatic regardless of whether the command file was read. Tools that back a flow which legitimately needs an agent reply keep their payload — `unmute` (`/vox model|provider` confirmations), `speak` (`/mute` acknowledgement), `notify` (`/vox c` voice listing), `record` (saved file paths), and the query tools `status`/`who`/`music_list` — so those flows retain both their data and permission to respond. The same change corrects the ♪ panel formatters, which had keyed off fields the tools never return (`music`/`music_play` read `.status`/`.style`/`.name`, `music_list` read `.tracks`) — so the panel had silently fallen back to a generic "♪ music updated" and a `?` album count. The formatters now read each tool's real schema: the panel line is the first line of the tool's authored `.message` (`music`, `music_play`, `music_next`), and the saved-album count comes from `.programs`. A bare-string guard ensures a FastMCP uncaught-exception error (surfaced as non-JSON text, not the `{"error":...}` contract) is still shown to the agent and never overwritten by the silence directive, so a tool failure is never reported as a silent success. Closes vox-lf6b.

### Fixed

- **Generated music pools no longer persist the session mood as a run-on tag or come back nameless (vox-5aom, vox-g58t)**: a pool's `vibe` tag stored the raw auto-vibe mood verbatim — a whole session narrative (`"a long brutal grind that shipped: 6 review rounds…"`, 40+ words with colons and em-dashes) — where a short, ID3-safe label belongs, and an unnamed pool's auto-name came back `null` instead of the documented `{vibe}-{style}-YYYYMMDD-HHMM` handle. The cause was shared: the unbounded vibe fed the auto-namer, which bailed rather than emit a 40-word slug. A new `VibeLabel` value type (`voxd/programs/vibe_label.py`) bounds any mood to a short, sanitized label — non-alphanumeric and control characters collapse to spaces, whitespace collapses, the result is capped at 48 characters on a word boundary, and it is empty when nothing usable remains (empty is a valid vibe; prose is not). It is applied symmetrically on the store (`AlbumTags`) and query (`TagQuery`) paths, so a resume on the raw mood still matches the stored bounded form. `AlbumTags.with_auto_name(created, taken)` now mints a guaranteed slug-safe `{vibe}-{style}-YYYYMMDD-HHMM` base name (a numeric suffix is appended when that base collides) from an injected clock (deterministic under a fixed clock), floors the style segment to `album` so the name always leads with an alpha token, and disambiguates the base name against the catalog's taken names via `mint_unique_name` — two same-`(style, vibe)` pools minted in the same clock-minute get distinct names, preserving the unique-`name` invariant `Catalog.by_name` relies on. Closes vox-5aom, vox-g58t.
- **Test suite no longer emits a `starlette.testclient` deprecation warning**: added `httpx2` to the dev dependency group so `starlette.testclient.TestClient` (used in `tests/test_voxd_health.py` and `tests/test_voxd_synthesis.py`) uses its preferred HTTP backend — `make test` now runs warning-free instead of surfacing `StarletteDeprecationWarning: Using 'httpx' with 'starlette.testclient' is deprecated; install 'httpx2' instead`.

## [4.12.0] - 2026-07-14

### Added

- **`vox voices` and stdin input on the CLI (vox-2vto)**: `vox voices [--provider X] [--json]` lists the voices the active (or given) provider offers plus the current session voice — previously you could *set* a voice (`vox voice NAME`) but had no way to *discover* one from the CLI (the capability existed only via the MCP `who` tool and `VoxClientSync.voices()`). And `vox say`/`vox record` now read the text from stdin — `echo "Build finished" | vox say`, or `vox say -` — so the CLI composes in shell pipelines.
- **Public Python API — `from punt_vox import VoxClient, VoxClientSync, SynthesisSpec, SynthesizeResult` (vox-iyqq)**: vox's WebSocket client is now a first-class, importable public API, not an internal detail you had to reach into `punt_vox.client` for. `punt_vox/__init__.py` exports the async `VoxClient`, the sync `VoxClientSync`, the `SynthesisSpec` request type, and the `SynthesizeResult` return type (with `__all__`; the package already ships `py.typed`), so a third-party program can drive the running `voxd` daemon directly — `VoxClientSync().synthesize("Build finished", SynthesisSpec(voice="sarah"))` to speak, or `.record(text, spec)` for MP3 bytes. Alongside the MCP server and the CLI, this makes all three of vox's surfaces usable outside a Claude Code plugin. `tests/test_public_api.py` locks the exported names as a permanent contract.
- **Python API — nameable failures, typed returns, a documented lifecycle (vox-1hfd)**: building on the client export above, the Python surface is now usable as a real public API, not just an import. **Failures are nameable**: `VoxError` is a new common base for `VoxdConnectionError` and `VoxdProtocolError`, and all three are exported — a caller writes one `except VoxError` to catch any client failure from the public namespace instead of reaching into the private `punt_vox.client_errors`, and every failure now raises a `VoxError` (a malformed daemon payload, previously a bare `ValueError` that escaped the contract, is wrapped as `VoxdProtocolError`). **Returns are typed**: `health()` returns a frozen `HealthStatus` and the `program_*` methods return `ProgramStatus`/`CommandOutcome`/`ProgramSummary` instead of bare `dict[str, Any]`, so a caller can discover the response shape from the type. The wire-observable value types live in neutral top-level modules (`types_health`, `types_programs`) shared by both the client and the daemon, so the thin client no longer reaches into the daemon's state-machine internals — `import punt_vox` pulls no Starlette/pydub/provider SDK (a test locks this invariant). **`PromptSet`** (the `program_on`/`program_select` prompt argument) is exported so authored prompts no longer need a private import. **`VoxClient` gained `async with`** support (connect on enter, close on exit). Client docstrings are rewritten for the API consumer, and `CommandOutcome`'s contract is documented honestly: in the current protocol commands are acknowledged at enqueue and `applied` is always True — surfacing a lost-race rejection to the caller is tracked as follow-up work. Closes vox-1hfd.
- **Vox self-registers an agent-facing usage guide via a CLAUDE.md `@`-import (vox-ys4z)**: `vox install` now writes `~/.punt-labs/vox/CLAUDE.md` — a focused usage manual for the `mic` MCP tools and the `/music`, `/vibe`, `/vox`, `/unmute`, `/mute`, `/recap` slash commands — and self-registers the line `@~/.punt-labs/vox/CLAUDE.md` in `~/.claude/CLAUDE.md` inside a shared `<!-- punt:mandatory-reading -->` section headed `## Tool Guidance (auto-loaded)`. Claude Code resolves the top-level `@`-import into every session's context regardless of the launch directory (verified empirically from four working directories, `~` resolved), so agents see how to drive vox in any project without a per-project edit. The installer owns and rewrites the guide every run, so it never drifts from the running version. The reconcile is deterministic (import lines sorted), corruption-safe (a lone or duplicated marker is repaired to one canonical section rather than appended), and writes `~/.claude/CLAUDE.md` only when the rendered text changes — no mtime churn on the shared global file. `vox uninstall` deletes the guide and prunes its import line — when that was the last import the whole section is removed — leaving all content outside the managed section unchanged (a file that had no section before install is restored byte-for-byte). The `@`-line is emitted at top level, never inside a code fence (fenced imports are not resolved). Split across two modules: `claude_md.py` (`GlobalClaudeImports` — the reconciler) and `guidance.py` (`VoxGuidance` — the installer). Teardown is self-healing: a failing guide unlink still prunes the import, so the `@`-line is never orphaned pointing at a deleted guide. The atomic write leaks no file descriptor on any error path (fdopen/write/replace) and orphans no temp file, and the reconcile round-trip preserves the file's final-newline state byte-for-byte. Closes vox-ys4z.

### Changed

- **Auto-vibe is now agent-driven, not derived from command exit codes (vox-ek1m)**: `/vibe auto` sets the session mood from the *conversation*, judged by the agent, instead of from a deterministic per-command signal. A non-blocking `UserPromptSubmit` hook injects a soft reminder every Nth prompt (N=5) — only in `auto` mode — nudging the agent to glance at how the session is going and set the vibe via the `vibe` tool if the mood has shifted (`[happy]` when flowing, `[focused]`/`[frustrated]`/`[weary]` when stuck, `[relieved]` after a fix). The interim attempt to read each Bash command's **exit code** from the `PostToolUse` hook is abandoned and its machinery deleted: Claude Code does not expose the exit code to `PostToolUse` hooks — the `tool_response` carries only `stdout`/`stderr`/`interrupted`/`isImage`/`noOutputExpected`, and the tool result is finalized *after* the hook runs — so the accumulator recorded nothing. The agent, which sees the whole session, holds the success/failure context a per-command hook never could. Deleted (forward integration, no shims): the exit-code accumulator (`vibe_window`, `vibe_mood`), the `PostToolUse` Bash hook and `BashPayload`, the `vibe_signals` config field, and the interim Z model + design doc. The earlier output-pattern classifier, the unused transcript watcher, and the dead mood-pitch chime machinery stay deleted; notification chimes are two flat tones (task done, permission prompt). Closes vox-ek1m.
- **CLI: `vox unmute` is renamed to `vox say`, and `--json`/`--verbose`/`--quiet` now work after the subcommand (vox-2vto)**: the synthesize-and-play command was named `unmute` — an unintuitive mute-word for the product's core verb, and `vox say` (what a user would actually type) did not exist while `vox speak` was a notification toggle. It is now **`vox say`**, with no backward-compat alias (Punt Labs has no installed base to migrate — forward rename per PL-PP-1). Separately, the global output flags were only accepted *before* the subcommand (`vox --json status`), so the far more natural `vox status --json` failed with a usage error — they now work in the post-subcommand position on `say`/`record`/`status`/`version`/`voices` (the pre-subcommand form still works). The MCP `unmute` tool and `/unmute` slash command keep their names for now; renaming them in lockstep so all three surfaces agree is tracked in vox-yn8u.
- **`check-coupling` gains the `--relax` audited waiver, reaching full parity with `check-oo`**: exposing a public API legitimately raises a module's coupling, but `check-coupling` was regression-only with no escape hatch — it would block a justified, under-ceiling increase with no sanctioned path forward (the "coupling stale-baseline deadlock"). It now has the same audited `--relax FILE --justify TEXT` waiver `check-oo` has: scoped to the current change (a relaxation recorded at or before the merge-base cannot bless a fresh regression), per-metric (relaxing `efferent_coupling` does not waive `public_names`), recorded in `.oo-coupling-audit.jsonl`, and refused without a justification, on an untracked file, or on a file that did not loosen. All three ratchets now share the identical audited-waiver contract. The change surfaced (and the first use waives) the irreducible coupling of the new public-API re-export in `punt_vox/__init__.py`, both metrics remaining well under the PL-CU-1 ceilings.
- **The OO-quality ratchet now measures against the merge-base baseline and is decomposed into a package (vox-wbx1)**: the enforcement tooling (`tools/oo_score.py`, now a thin entry over the new `tools/oo_ratchet/` package) no longer compares current code against the baseline *in the same tree* — it compares **code-at-HEAD against the baseline at the PR's merge-base**, so a change can improve code and lock in its baseline in one commit and a squash-merge no longer erases the improvement. `make update-oo` is now scoped to the changed files (symmetric with `--check`) and provably never loosens; `--reconcile` is the explicit whole-tree sweep; `--relax FILE --justify` is the audited path for an accepted regression (the waiver is scoped to the current change, so a stale relaxation can't license a future one). The check also enforces baseline completeness and lock-in, carries a renamed file's history so a regression can't launder through a rename, and **fails closed** on any git error. This is the tooling half (PR 1) of the ratchet redesign specified in `docs/oo-ratchet-improvements.md`; CI enforcement + branch protection follow in PR 2.
- **The coupling and suppression ratchets reach merge-base parity with `check-oo` and are enforced in CI (vox-26i1)**: `check-coupling` (`tools/oo_coupling.py`, now a thin entry over the new `tools/coupling/` package) and `check-suppressions` (`tools/suppression_ratchet.py`, now over `tools/suppression/`) previously scoped their touched set to the *last commit* (`HEAD~1..HEAD`) and read the baseline from the *worktree* — so a coupling/suppression regression introduced in an earlier commit of a multi-commit PR slipped through CI, and any regression could be laundered by hand-editing the in-tree baseline in the same PR. Both now scope to the whole PR (`--base-ref <merge-base>..worktree`) and read the baseline from the **base commit** (`git show <base>:.oo-coupling-baseline.json` / `.suppression-baseline.json`), closing both the per-commit hole and the hand-edit-launder path; both **fail closed** on a git error, a corrupt or empty baseline, or an unresolvable base under `--require-base`. `check-suppressions` — previously in `make check` but never run in CI — is now wired into the CI `lint` job alongside `check-coupling`, each threading `COUPLING_BASE`/`SUPPRESSION_BASE` (merge-base + `--require-base` on PRs, `HEAD~1` on push:main). The two 900+/350-line tool monoliths are decomposed into focused packages that pass their own OO self-check, and all three ratchets (`oo_ratchet`, `coupling`, `suppression`) now share one identical fail-closed, base-commit-authoritative contract. Closes vox-26i1.

### Fixed

- **Auto-vibe no longer resolves to "frustrated" on successful sessions (vox-p0u6)**: the signal classifier that drives `/vibe auto` was matching bare case-insensitive substrings — `FAILED`, `AssertionError`, `ERRORS?` — against only the first 500 characters of a Bash command's output, and ignoring the exit code. So any successful command whose output merely contained the word "error" or "failed" was logged as a test failure: a green `make check` that named the file `client_errors.py`, a `git commit` of an error-handling change, a `gh pr` with "error" in the title. Successful work accumulated a pile of `tests-fail` signals and the mood resolved to `[frustrated]` every session — a headline feature silently lying about how the work was going. The classifier is rebuilt (`command_signal.py`, `CommandSignal`) so the **exit code is authoritative** (a command that exits 0 can never produce a failure signal, and a structured `N failed` / `Found N error` / `CONFLICT` in the output still wins over a masked-exit pipeline like `pytest | tee`), markers are **anchored to structured summary tokens** rather than incidental words, and the **tail** of the output is scanned so pytest's end-of-run `N passed` summary is actually seen. Unrecognized commands emit no signal rather than a guessed one. Notably, the bug had been *protected* by tests that asserted the wrong behavior (`"AssertionError" → tests-fail`); those were inverted to the correct contract and the six audited false positives are pinned as regression tests, alongside an end-to-end test that a productive session resolves to a non-frustrated vibe. Closes vox-p0u6.
- **check-oo and check-suppressions reach full fail-closed / never-loosen parity with check-coupling (surfaced by the ratchet Z model)**: a fuzz-checked Z specification of the three ratchet tools (`tools/docs/ratchet.tex`) documented current behavior and, in doing so, surfaced three places where the OO and suppression ratchets still lacked hardening `check-coupling` already had. All three are now closed: (1) `oo_ratchet`'s base-commit and in-tree baseline readers reject a non-dict, non-numeric, boolean (an `int` subclass comparing as 0/1), or non-UTF8 baseline as a controlled non-zero — previously an ill-typed-but-parseable baseline slipped past `json.loads` and either crashed with an uncaught `TypeError` or skipped every metric via a `metric not in "<string>"` substring test (a fail-open); (2) `check-oo` fails closed on an empty `{}` base baseline under `--require-base`, where a pure-add change could previously pass as an ordinary success; (3) `check-suppressions --update` refuses to write a higher total (never-loosen) instead of writing the current count unconditionally, so the baseline can no longer be silently loosened. All three tools now genuinely share one fail-closed, base-commit-authoritative, never-loosen contract; the Z spec's Findings section records the closure. The type-hardened reader (1) is a prerequisite for the empty-baseline intercept (2) — an untyped reader could return `{}` on garbage and reopen the fail-open.
- **Manifest saves are race-safe, and the secrets writer is consolidated onto one atomic path (vox-djua)**: `voxd/programs/filesystem_store.py` saved a program manifest through a *fixed* temp name (`.manifest.json.tmp`), so two concurrent saves could collide on that path and corrupt the write; it now routes through the shared `AtomicFile`, whose `mkstemp` gives every writer a unique temp name. The API-keys writer (`service/keys_env.py`) also moves onto `AtomicFile` — `replace()` gained an explicit `mode` parameter so `keys.env` is still forced to `0600` even over an existing wider file, and the consolidation deletes the module's duplicate `mkstemp`/`fchmod`/`fsync`/`replace` block. The shared writer's cleanup now covers `BaseException` rather than only `OSError`, closing a class where an interrupt mid-write could orphan a temp file holding plaintext secret bytes. The two binary/streaming writers (`cache.py`, `providers/elevenlabs_music.py`) are deliberately left separate — a text-mode atomic writer would corrupt binary audio. Closes vox-djua.

## [4.11.0] - 2026-07-09

### Security

- **`vox install-desktop` no longer writes the provider API key in plaintext into `claude_desktop_config.json` (PL-PP-4)**: the command previously resolved `ELEVENLABS_API_KEY`/`OPENAI_API_KEY` and embedded it as `"env": {"ELEVENLABS_API_KEY": "sk_..."}` in a file that is world-readable in practice, cloud-synced, and captured in backups — spraying a long-lived secret into an unprotected config. The MCP server (`vox mcp`) is a thin WebSocket client of `voxd` and never needs the key; the daemon reads it from `keys.env` (mode 0600, in the 0700 state dir) at startup. The registration now carries only non-secret routing config (`TTS_PROVIDER`, `VOX_OUTPUT_DIR`), and the new `DesktopInstaller` (`desktop_install.py`) verifies the daemon can reach the credential from `keys.env` — the daemon's only key source, since a detached service never inherits the installing shell's environment — warning, without ever echoing the value, when it cannot (naming the variable and pointing at `vox daemon install`). No migration of key-bearing configs: the secret is simply never written going forward, and an overwritten `vox` entry drops it.

### Added

- **Per-vibe music pools — an album library keyed by style and vibe (vox-q7vh)**: music pools are now first-class **albums** you accumulate, not a single mutable pool. Every album has a stable id; `style`, `vibe`, and `--name` are *queryable tags*, so you can keep arbitrarily many albums for the same `(style, vibe)` — twelve different "trance / calm" variants side by side. `/music on` with no argument resumes the newest album matching your current vibe (or mints one); `--name` saves or replays a specific album (names are unique, auto-suffixed). Replaying by tags — `/music play <style> <vibe>` — assembles every matching album's tracks into an uncapped "radio" that rotates at zero credits and **generates nothing**. Re-authoring a pool's prompts mints a *fresh* album instead of mixing old and new tracks — a prompt fingerprint is part of album identity (closes vox-1uo5). Internally an in-memory `Catalog` over the on-disk manifests replaces the per-command directory scan, and playback is split behind a `PlaybackSource` protocol into the existing generate-and-fill `Program` and a new consume-only `SelectionPlayback` — the generate state machine is unchanged. Legacy single-pool directories are ignored by tag queries, never migrated. Closes vox-q7vh.
- **Audio Programs — persisted, replayable, ownership-free music pools (vox-oayr)**: the background-music subsystem is rebuilt on a first-class **Program** model. `/music on [style X]` generates a pool of up to 12 distinct instrumental tracks, plays the first at once, fills the rest in the background, and rotates the full pool at zero credits — and the pool is now **saved to disk and replayable**: `/music play <name>` / `vox music play <name>` (plus `list`, `next`, `loop`, and `playlist:2` part-addressing) drive a saved pool from the CLI or MCP with no generation. Pools live at `~/Music/vox/<name>/` (named by `--name`, else the style) with **ID3 tags** (artist/album/title/genre/track) on every track, so they drop straight into macOS Music.app and Ubuntu players. Control is **ownership-free** — any client (MCP session or CLI, from any process) drives any command against the single daemon-wide Program; the old session-ownership model is gone. Internally the 29-method `MusicScheduler` god-facade and the `voxd/music/` package dissolve into a single-writer `ControlChannel` + typed `ControlSignal`s, a `ProgramState` value object whose 16 invariants hold by construction (an executable Z model, `docs/audio-programs.tex`), a single-flight `Filler` bounded on permanent generation failure, and a `ProgramLoop` that auto-advances on track end. Player-spawn failures surface through the `status` API (`playback_error`), not only the daemon log. Closes vox-oayr.

### Changed

- **`/music` prompt authoring now forbids named artists/composers/titles**: ElevenLabs Music rejects prompts that reference a specific artist, composer, band, or copyrighted work under its Terms of Service (`bad_prompt`, no track generated). The `/music` command guidance now bans them explicitly and shows how to evoke the same sound by describing form/instruments/mode/era instead ("romantic-era nocturne in E-flat" not "Chopin nocturne"). Prevents the silent "music won't start" failure hit when a classical-piano pool named composers. The systemic fix — surfacing generation failures through the `status` API instead of only the daemon log — is specced in `docs/vox-ig52-music-resilience.md` (vox-ig52).

### Removed

- **Retired the one-time migration paths and their legacy shims**: the audio system has fully moved to saved Programs under `~/Music/vox` and a system-level `voxd.service`, so the transitional code that bridged the old layouts is gone. Dropped: the `vox music migrate` command and its `LegacyMigration` (the `tracks/ → programs/` importer, plus the daemon's start-up hint and `dirs.music_output_dir`); the `vox migrate-audio` command and its `AudioMigration` (the `~/vox-output → ~/Music/vox` mover); the `doctor` legacy checks that flagged `~/vox-output` and a stale user-level `vox.service` (and their now-dead parse/validate helpers); the systemd backend's stale user-unit auto-removal (`cleanup_stale_user_unit`/`legacy_user_unit_path`); the `logging_config.VOX_DATA_DIR` re-export (callers now import `paths.user_state_dir`); and the `config` module-level `read_config`/`read_field`/`write_field`/`write_fields` wrappers (callers now use `ConfigStore` directly). No supported workflow depends on these; the removals shrink `doctor.py` (−134 lines), `service/systemd.py` (−57), and `config.py`'s procedural surface (method_ratio 0.35 → 0.46).

### Fixed

- **Server-tool tests are hermetic — they never read or mutate the real `voxd` (vox-73m5 class)**: `tests/test_server.py::TestStatusTool::test_defaults_when_no_state_set` (and three sibling `status`-reading tests) called `status()` without redirecting the daemon-facing seam, so the tool connected to the *live* daemon and read its real `music_mode`. `make check` passed only because CI runs with no reachable `voxd` (the label defaults to `off` when unreachable); with a daemon actually playing music the test read `on` and failed — release-blocking and non-deterministic. A new autouse `_hermetic_daemon` fixture installs a clean *idle* `FakeProgramGateway` on `_program_tools` and an *unreachable* client on `_voxd_client` for every server-tool test, so no test can reach the real daemon by default; tests needing a specific Program state override the seam locally as before. A `TestSuiteDoesNotTouchRealDaemon` guard (mirroring `TestSuiteDoesNotTouchRealConfig`) fails loudly if the redirect ever regresses.
- **`vox daemon restart` no longer fails its first invocation on macOS (vox-kne8)**: a restart booted the running daemon out of launchd and then re-bootstrapped it, but `launchctl bootout` is asynchronous — it returns before launchd finishes unregistering the job. The re-`bootstrap` raced that teardown and failed with `Bootstrap failed: 5: Input/output error`, leaving voxd DOWN; only a second `vox daemon restart` (after the stale registration cleared) succeeded. The launchctl domain control is extracted into a new `LaunchctlAgent` (`service/launchctl.py`) that makes bring-up idempotent: `bootout` now **waits** (bounded, polling `launchctl print`) for the job to actually leave the `gui/<uid>` domain, and `bootstrap` waits for a clear domain before running and **retries once** on a residual exit-5 after re-confirming the job unregistered. Both `vox daemon install` and `vox daemon restart` share this one race-free path. If the job never clears or bootstrap keeps failing, the restart now **exits non-zero with a clear message** (never a false "restarted") — the client-observable contract. Extraction also shrinks `service/launchd.py` (160 → 134 lines) and `daemon_restarter.py` (174 → 165). Closes vox-kne8.
- **`vox install` degrades gracefully when the daemon registers but never becomes healthy**: the best-effort marketplace `install` command handled launchd *bring-up* failures but not the post-registration case — a daemon the service manager registered yet that never answered its health check within the deadline (bad env, port contention, a broken `voxd`). `ServiceInstaller` raised a bare `RuntimeError` that the command's `except` tuple did not catch, so `vox install` aborted with an uncaught traceback instead of the intended "Skipped — vox works without it" (exit 0). The post-install health poll is extracted into a `HealthVerifier` (`service/health_verify.py`) that raises a typed, sealed `ServiceHealthError`, which `install` now catches. `vox daemon install` still fails **loudly** (exit 1) on the same condition — only the best-effort marketplace install degrades. The extraction also relieves `installer.py` (252 → 207 lines).
- **The test suite no longer overwrites the developer's real vox config**: `DEFAULT_CONFIG_DIR` is a *relative* path (`.punt-labs/vox`) and `find_config_dir()` walks up from the cwd, so tests that drove a config-writing path without redirecting the dir — the `vibe` MCP tool, the `/vibe` CLI command — clobbered the live `.punt-labs/vox/vox.local.md` on every `make test`, silently resetting the session vibe (the "always reverts to sad" symptom, since fixtures wrote `vibe: "sad"` / empty into it). An autouse `hermetic_config` fixture now redirects every *ambient* config resolution — `ConfigStore(None)`, `server._find_config_dir()`, and the `__main__` CLI commands — to a per-test tmp dir, so no test can read or write the real config through the default path. A `test_suite_does_not_touch_real_config` regression guard asserts the redirect stays active and that the real config bytes are untouched after driving the default read/write paths.
- **Audio Programs hardened through pre-merge review (vox-oayr)**: seven correctness bugs in the new Program subsystem were caught by security/Bugbot review and fixed before merge. A **path traversal** via `.`/`..` in program names — `ProgramName` now rejects dot components and path separators, with a resolved-path guard in `FilesystemProgramStore`. `playlist:N` addressing and the "part N of M" status resolving by **list position instead of the intrinsic manifest index**, so a gap from a failed part played or reported the wrong track. A **retry-machine gap**: a transient generation error stranded the fill in `retrying` forever (the reconciler cancelled the retry engine), and an at-cap non-empty pool had no valid transition (`retry_capped` added; the reconciler keeps the fill alive while retrying). A **stale-fill orphan race**: a generation completing behind a program switch polluted the switched-in pool — fill outcomes are now tagged with their origin Program and discarded at apply if it changed. And the MCP `status` payload no longer carries a mutable `music_mode` shadow that could contradict the authoritative `program.mode`; it is derived per call.

## [4.10.0] - 2026-07-05

### Added

- **Background music is now a self-driving playlist**: `/music on` plays the first track the moment it is ready, then generates the rest of the pool (up to 12) in the **background with no commands**, and **auto-advances** to a different track as each one ends. Once the pool has 12 it stops generating and **rotates** (shuffle, never back-to-back) forever at zero credits. A vibe/style change finishes the current song, then switches to that vibe's pool. This replaces bas7's manual-skip rotation, which only advanced on `/music next` and otherwise looped a single track. Internally the loop was rebuilt (`MusicLoop` auto-advance + a scheduler-owned cancellable `PoolFiller` + a `Playlist` selection unit), and all disk access now sits behind an injected `TrackStore` protocol (filesystem impl in production, in-memory fake in tests). Behavior is proven by loop-level tests that drive the real playback loop. Closes vox-1rxb (rebuild of #291/bas7).
- **Background music now rotates a playlist instead of regenerating every time**: once a `(vibe, style)` has 12 saved tracks, `/music next` and vibe changes shuffle through the existing pool (never repeating the just-played track) with **zero** ElevenLabs credits, instead of generating a fresh track each time. Below 12 it still generates (lazy fill). And generated tracks are now genuinely distinct — each of the 12 gets a different musical variation descriptor (analog pads, arpeggios, sub-bass, Rhodes, lo-fi tape, four-on-the-floor, woodwinds, …), so the pool spans the sonic space rather than 12 near-clones. Fixes the "too repetitive" complaint. Closes vox-bas7.
- **Cache hit/miss is now observable**: the `unmute` MCP result (and the internal `playing` response) carries a `cached` boolean, and `voxd` logs a distinct `cache HIT` / `cache MISS` line at INFO with the cache file path. A second identical request (same text+voice+provider) is served from cache with `cached: true`, so callers — and the manual test flight (Step 2) — can confirm a cache hit directly instead of grepping the daemon log for a missing `Synthesize:` line. Caching behavior is unchanged; this is observability only. Closes vox-90vw.

### Changed

- **Ethos is now project-local, not a git submodule**: `.punt-labs/ethos/` was a `punt-labs/team` submodule; it is now a vendored project-local directory trimmed to the 15 identities vox delegates to, plus the engineering team and the durable mission records (`missions/` + `missions.jsonl`; verbatim per-session activity logs are gitignored). Removes `.gitmodules`. Vox owns its identity data rather than depending on the shared team submodule, so the repo is self-standing. (Ethos still *resolves* identities from the global `~/.punt-labs/ethos/` registry; repo-primary resolution is a pending ethos-side change coordinated with the ethos agent.)
- **Health-check target extracted and hardened**: the post-install health poll's `HealthTarget` (host/port/token resolution) moved out of `service/installer.py` into a focused `service/health.py` module (relieving `installer.py`, which sat at the OO `module_size` limit), and now validates its platform domain at construction — an unexpected value (e.g. a miscapitalized `"Linux"`) can no longer silently skip the systemd bind-gate and false-fail against a healthy daemon; it raises `ValueError`. Internal refactor plus latent-bug hardening; no behavior change on supported platforms. Closes vox-84ft.
- **Service platform domain is single-sourced and drift-proof (internal)**: the `macos`/`linux` platform is now one `PlatformName` type alias (`service/types.py`) threaded end-to-end, with `assert_never` exhaustiveness at every platform branch — adding a future third platform becomes a compile error instead of a silent mis-route (e.g. an un-narrowed value starting the daemon under the wrong init system). Also rehomed the `_voxd_exec_args` helper onto `ServiceInstaller`, restoring `installer.py`'s OO ratios. Behavior-preserving. Closes vox-376k, vox-n0b6.

### Fixed

- **Session vibe no longer reverts to a stale mood**: `vibe_mode` lived in the git-tracked `vox.md` (committed as `manual`), so any `git checkout`/`stash`/`reset`/branch-switch — constant during agent sessions — reverted it, silently resurrecting `manual` while the gitignored mood (`sad`) was never cleared; separately, the MCP `vibe` tool cleared only tags, not the mood, on `/vibe auto`/`off` (diverging from the CLI). A vibe change is now authoritative: the whole vibe cluster (mode, mood, tags, signals) moved to gitignored ephemeral config so git can't resurrect it, `ConfigStore.read()` loads only each file's own keys (a committed `vibe_mode` is inert), and a new `VibeChange` value object owns the transition rules so `/vibe auto`/`off` fully clear the mood — matching the CLI. Hooks only ever write `vibe_signals`/`vibe_tags`, never `vibe`/`vibe_mode`. Closes vox-73m5.
- **`/music on` gives a clear message when no ElevenLabs key is set**: background music generation is hard-wired to ElevenLabs, so with no usable `ELEVENLABS_API_KEY` the pool fill used to fail silently — the daemon retried, backed off, and disabled music, leaving only a stack trace in `voxd.log` while the user saw music quietly turn off. Turning music on now preflights the key and returns `Background music requires an ElevenLabs API key (set ELEVENLABS_API_KEY)` to the user immediately, instead of the silent attempt-then-disable. Replaying an already-saved track still works with no key, and a present key that later hits a rate/quota limit still goes through the existing retry/backoff. Closes vox-1rxb (robustness follow-up).
- **`vox daemon uninstall` no longer reports success when a daemon survives**: the uninstall path discarded `kill_stale_daemon()`'s result, so a failed kill (e.g. a `PermissionError`, or a daemon owned by another user) left `voxd` running while the command still reported success. Uninstall now re-scans the port and exits non-zero if a `voxd` daemon survives — distinguishing a genuine survivor from an already-stopped daemon (an empty port stays a clean success, so a normal uninstall doesn't false-fail). Closes vox-qogn.
- **Chime spawn failures now log the underlying error**: `_chime_via_voxd`'s `OSError` handler logged a bare "Could not spawn chime subprocess" with no context; it now includes the errno/strerror (`"…: %s", e`) so the failure is diagnosable. Closes vox-wqft.
- **Port-probe tool failures are no longer silent**: `find_pid_on_port` swallowed `lsof`/`fuser` spawn/timeout errors the same as an empty port, so a failed probe silently reported "no process" — masking a surviving daemon in the uninstall survivor-check and `ensure_port_free`. It now logs a WARNING (tool name + errno) on a genuine tool failure while keeping the common empty-port path quiet, and its PID parse guard uses `isdecimal` so it's total (a non-decimal token can't slip through to `int()`). Closes vox-sgmc.
- **Hooks no longer crash on config I/O errors**: `handle_post_bash` and `handle_session_end` now guard their `read_config`/`write_field` calls — a corrupt or unwritable `vox.local.md` logs a warning and the hook returns cleanly instead of raising a non-zero exit that could block the Claude Code tool it gates. Closes vox-nb7i.
- **Unexpected hook stdin read failures are now logged**: `_read_hook_input` logs any genuine `OSError` (with its errno) at WARNING instead of silently returning `{}`; the expected empty/closed-pipe case (no errno) stays quiet. Closes vox-gsh4.

## [4.9.0] - 2026-07-03

### Added

- **`/music next` command**: skip to a new generated track while the current one keeps playing (gapless). New `music_next` WebSocket message, MCP tool, and CLI subcommand. Closes vox-n3me.

### Changed

- **Dev tooling moved to PEP 735 `[dependency-groups]`**: `mypy`, `pyright`, `ruff`, `pytest`, `pytest-asyncio`, and `pytest-cov` now live in `[dependency-groups].dev` instead of `[project.optional-dependencies].dev`. A plain `uv sync` installs them automatically — `--extra dev` is no longer needed (and no longer accepted). `lux` remains an end-user extra. CI's `uv sync --frozen --all-extras` continues to install everything, since uv auto-includes the `dev` group unless `--no-dev` is passed.

### Fixed

- **macOS daemon 7x slower than manual launch**: `voxd` was installed as a LaunchDaemon (`/Library/LaunchDaemons/`), which macOS throttles with background QoS. Moved to a user LaunchAgent (`~/Library/LaunchAgents/com.punt-labs.voxd.plist`), eliminating CPU/IO throttling and every sudo requirement — macOS install *and* uninstall are now fully sudo-free. No automatic LaunchDaemon→LaunchAgent migration ships (only a handful of pre-release installs exist); the old system plist on those machines is removed once, by hand: `sudo launchctl bootout system /Library/LaunchDaemons/com.punt-labs.voxd.plist; sudo rm -f /Library/LaunchDaemons/com.punt-labs.voxd.plist`. Closes vox-wt79, vox-zt3r.
- **MCP server state went stale after CLI config writes**: the MCP server seeded `SessionState` from config at startup but never re-read. CLI commands like `vox vibe auto` wrote to `vox.local.md` but the MCP server retained the old values, causing `/music on` to report stale mood. All MCP tools now call `_refresh_state_from_config()` to re-read config before acting. Closes vox-duw.
- **Music restart glitch when `/music on` called while playing**: `_handle_music_on` killed the current playback subprocess before signaling the music loop, causing a stop/pause/restart gap. Now skips the kill when the same owner re-sends `music_on` — the existing gapless handoff path generates the new track while the old one keeps playing. Closes vox-rqc.
- **Hooks announced the wrong repo name**: spoken notifications derived the repo from the hook process's git directory (`git rev-parse --git-common-dir`), so a session in `punt-labs/vox` could announce "punt-labs" (the parent folder, itself a git repo). The repo name now comes from the session's working directory (`cwd`) that Claude Code puts on hook stdin. Shell scripts extract cwd and gate on the repo's own `.punt-labs/vox/vox.md`; repos without their own config stay silent. The Python layer resolves config via `find_config_dir(cwd)` and derives the spoken name from the cwd's git root (not the config directory). Also removed the dead `config.md` gate (config split into `vox.md` + `vox.local.md` in 7e151b1) and the dead daemon-relay branch (targeted a non-existent voxd `/hook` endpoint). Closes vox-q6v2.

## [4.8.1] - 2026-05-12

### Fixed

- **Long audio cut off at 30 seconds**: `_PLAYBACK_TIMEOUT_DEFAULT_S` in voxd was 30s, used as the fallback when ffprobe can't determine audio duration. Long `/recap` summaries producing >30s of audio were killed mid-sentence. Raised to 120s. Music playback is unaffected (separate playback path with no timeout).

## [4.8.0] - 2026-05-12

### Added

- **Remote voxd connectivity**: client-side env vars `VOXD_HOST`, `VOXD_PORT`, `VOXD_TOKEN` override localhost/file-based discovery, enabling cross-host audio playback without SSH tunnels. Server-side `VOXD_BIND` (via `--host` flag or env var) controls the bind address (default `127.0.0.1`, opt-in `0.0.0.0`). Token auth remains the security boundary. `vox doctor` reports active env var overrides. Access logs redact auth tokens. Warning logged when binding to non-localhost. See `docs/guide-remote-setup.md` for setup guide.
- **Repo name in spoken notifications**: hook quips now prefix the repo name (e.g. "vox. Needs your approval.") so users with multiple simultaneous sessions can identify which project needs attention. Derived from the config directory path. Degrades gracefully when config_dir is unavailable.

### Fixed

- **`make check` missing markdownlint**: added `docs` target running `npx markdownlint-cli2` to match the CI docs job. PRs that pass `make check` locally will no longer fail the docs CI check.

## [4.7.6] - 2026-05-12

### Security

- Bump authlib 1.6.9 → 1.6.11: CSRF fix in Starlette OAuth client, unvalidated `redirect_uri` on UnsupportedResponseTypeError (#236)
- Bump python-multipart 0.0.22 → 0.0.27: multipart header limits (DoS hardening), `chunk_size` validation (#233)
- Bump python-dotenv 1.2.1 → 1.2.2: symlink following fix in `set_key`/`unset_key` (#234)
- Bump pytest 9.0.2 → 9.0.3: insecure temporary directory CVE-2025-71176 (#235)

### Removed

- Dead `assets` symlink at repo root (pointed to `src/punt_vox/assets/`, unused since v3.0.0) (#241)

## [4.7.5] - 2026-05-11

### Changed

- **Config split into two files**: per-repo config moved from single `.vox/config.md` to two files under `.punt-labs/vox/`: `vox.md` (tracked in git, durable preferences: voice, provider, model, notify, speak, vibe_mode) and `vox.local.md` (gitignored, ephemeral session state: vibe, vibe_tags, vibe_signals). Removed legacy `.vox/` directory entirely. The `config_path` parameter is now `config_dir` throughout the API. Read/write helpers route fields by `DURABLE_KEYS`/`EPHEMERAL_KEYS` frozensets. `find_config()` renamed to `find_config_dir()` in new `dirs.py` module.

### Fixed

- **`/music` triggered permission prompt on first use**: `hooks/session-start.sh` auto-allows `Skill()` rules for plugin commands, but `Skill(music)` was missing from the hardcoded list. Added it, and introduced `scripts/check-skill-permissions.sh` (wired into `make lint`) to flag any drift between `commands/*.md` and the hook's allowlist. Closes vox-zz2.

## [4.7.4] - 2026-04-14

### Changed

- **ElevenLabs**: re-enabled `eleven_v3` in expressive models set — bracket-style tags like `[alert]` and `[serious]` are now preserved and interpreted as expressive cues instead of being stripped. The v4.7.1 removal was based on a misdiagnosis; the CLI was pre-normalizing brackets before the model saw them.

## [4.7.3] - 2026-04-14

### Fixed

- **CLI double-normalization defeated vibe tag stripping**: `vox unmute` and `vox record` called `normalize_for_speech` before sending text to voxd. This stripped brackets from vibe tags (e.g. `[alert]` → `alert`) before voxd's `_apply_vibe_for_synthesis` could match and remove them. Result: bare words "alert serious" survived into ElevenLabs. Fix: removed pre-normalization from both CLI commands — voxd already normalizes via `_apply_vibe_for_synthesis`. Closes vox-6kv.

## [4.7.2] - 2026-04-14

### Fixed

- **Trailing vibe tags spoken as literal words**: when vibe tags appeared at the end or middle of text (e.g. `"Wall from claude: hello [alert] [serious]"`), `normalize_for_speech` stripped the brackets but left the bare words "alert serious" in the TTS input. Root cause: `_apply_vibe_for_synthesis` only split *leading* tags before normalization — trailing and inline tags survived into `normalize_for_speech` which strips brackets but not content. Fix: strip all `[tag]` patterns at any position *before* normalization on non-expressive models. Expressive model path also fixed to normalize text around tags at any position, preserving them for future re-enablement. Closes vox-6ls.

## [4.7.1] - 2026-04-14

### Fixed

- **Vibe tags spoken literally by ElevenLabs**: `[serious]`, `[alert]`, and other bracket-style expressive tags were passed through to ElevenLabs `eleven_v3` as literal text instead of being stripped. The model does not interpret them as expressive cues — it speaks "[serious]" aloud. Emptied `_EXPRESSIVE_MODELS` so `strip_vibe_tags` runs unconditionally for all ElevenLabs models.

## [4.7.0] - 2026-04-12

## [4.6.0] - 2026-04-12

### Added

- **Directory migration from `.vox/` to `.punt-labs/vox/` (vox-4jk)**: per-repo config now lives at `.punt-labs/vox/config.md` (was `.vox/config.md`). Saved audio output defaults to `~/Music/vox/` (was `~/vox-output/`). Music tracks live at `~/Music/vox/tracks/` (was `~/vox-output/music/`). New `dirs.py` module centralizes all cross-platform path resolution. Auto-migration runs on `vox install` and `vox daemon install`; shell hooks check both paths during transition. `vox migrate-audio` command moves saved audio from `~/vox-output/` to `~/Music/vox/` with dry-run by default (`--execute` to move). `vox doctor` checks for legacy `.vox/` directory and `~/vox-output/` with remediation hints.

### Changed

- **Default output directory**: `default_output_dir()` now returns `~/Music/vox/` instead of `~/vox-output/`. `VOX_OUTPUT_DIR` env var override still works.

## [4.5.1] - 2026-04-11

## [4.5.0] - 2026-04-11

## [4.4.0] - 2026-04-11

### Added

- **Background music generation (`/music on|off`, `vox music on|off`)**: vibe-driven instrumental music that loops during coding sessions. When music mode is on, vox generates ~2-minute tracks via the ElevenLabs Music API using the current session vibe, style modifier, and time-of-day context, then loops them at reduced volume through voxd while speech and chimes play at full volume on top. When the vibe changes (via `/vibe` or auto-vibe), a new track generates to match. `/music on style techno` sets a persistent style preference; `/music off` stops playback. Session ownership ensures only the controlling session's vibe drives the music. State is daemon-wide and ephemeral (daemon restart = music off). Typical credit usage: 1-3 tracks per session (~2k credits each). Requires ElevenLabs paid plan. Includes CLI commands (`vox music on [--style ...]`, `vox music off`), MCP tool (`music`), slash command (`/music`), vibe-to-prompt mapping, `MusicProvider` protocol, `ElevenLabsMusicProvider`, `MusicLoop` async task in voxd, and dedicated playback subprocess separate from the speech/chime queue. Closes vox-0qi.

### Changed

- **`vox doctor --json` rows now include `status_kind` field (vox-kl7)**: each check row carries `status_kind` with values `"pass"`, `"warn"`, `"fail"`, or `"skip"` so machine consumers can distinguish warnings from hard failures. The existing `passed` boolean is unchanged.

### Fixed

- **Qualified "world-readable" and "/proc" references in comments (vox-t2f)**: replaced bare "world-readable" with "others-readable (mode & 0o004)" in `service.py` keys.env race comments, and added "(Linux-specific; macOS has no /proc)" to the `/proc` reference in `voxd.py`, for cross-location consistency with PR #175's broader phrasing.

## [4.3.2] - 2026-04-11

### Fixed

- **voxd `_ws_route` logged a full ERROR traceback on every chime (vox-ewh)**: after the vox-ehf fix in 4.3.0, chime/unmute clients return on the `"playing"` ack and close the WebSocket while voxd's receive loop is still awaiting the next `receive_text()`. The trailing `contextlib.suppress(WebSocketDisconnect, RuntimeError)` sends of the stale `"done"` message inside `_handle_synthesize` and `_handle_chime` land on the peer-closed socket, transition Starlette's `application_state` to `DISCONNECTED`, and swallow the resulting `WebSocketDisconnect(1006)`. The next `receive_text()` in the outer loop then observes `application_state != CONNECTED` and raises `RuntimeError('WebSocket is not connected. Need to call "accept" first.')` — not `WebSocketDisconnect` — so the narrow `except WebSocketDisconnect:` branch missed it and the loop fell through to `except Exception: logger.exception("WebSocket error")`, emitting a multi-line traceback on every `/recap`, every stop-hook chime, and every prompt chime. On the reporter's box this was filling the journal with hundreds of spurious error entries and burying real failures in the same unit slot. Fix preempts the RuntimeError at its source: `_ws_route` now checks `websocket.application_state` at the top of the receive loop and `break`s cleanly when it is no longer `WebSocketState.CONNECTED`, so `receive_text()` is never called against a disconnected socket. The outer `except WebSocketDisconnect:` clause stays exactly as narrow as it was pre-fix — the `except Exception` branch still catches any genuine unexpected `RuntimeError` from `receive_text`, `json.loads`, or a handler, preserving real error visibility. Two regression tests in `tests/test_voxd.py::TestWsRoutePeerClose` lock in the narrowing: one drives `_ws_route` with a fake WebSocket whose `application_state == DISCONNECTED` and asserts the loop breaks without calling `receive_text` and without logging a `"WebSocket error"` record; the complement drives a fake WebSocket whose `application_state == CONNECTED` but whose `receive_text` raises an unrelated `RuntimeError`, and asserts exactly one `"WebSocket error"` record is logged — documenting that the fix catches only the peer-closed-state case and nothing else. Audio playback, client acks, and dedup were all unaffected — this is purely log-spam cleanup. Closes vox-ewh.

## [4.3.1] - 2026-04-11

### Fixed

- **Stale user-level `vox.service` crash-looping on legacy `vox serve` entrypoint (vox-45r)**: an earlier install layout registered `~/.config/systemd/user/vox.service` with `ExecStart=.../vox serve --port 8421`. The `serve` subcommand was removed during the voxd/voxd.service split — later installs did not clean up the user-level file, and systemd's `Restart=on-failure` respawned the unit every 5 seconds against a CLI that exits with `No such command 'serve'`. Observed in the field at restart counter 107,069 over a 9-day boot window (~12 restarts/minute), filling the journal with hundreds of thousands of spurious lines and obscuring real failures in the same unit slot. The currently-running daemon is the system-level `voxd.service` at `/etc/systemd/system/voxd.service`; the user-level file is pure legacy. Fix is three parts: (1) `vox daemon install` (and `vox install`, which wraps it) now detects the stale user unit on Linux and removes it via `systemctl --user disable --now vox.service`, unlink, `systemctl --user daemon-reload` — idempotent, user-writable, no sudo, no-op on machines that never had the legacy unit. The cleanup runs before `_systemd_stop` / `_ensure_port_free` so a recovering install clears the crash-looping unit first. Scope is strictly `~/.config/systemd/user/vox.service`; the system-level `voxd.service` is never touched. (2) `vox doctor` gains a regression check that parses `ExecStart=` in the user unit (if present) and fails loudly when the referenced subcommand is not in the current CLI command set. Remediation hint points at `vox install` (which now cleans up automatically) and a manual `systemctl --user disable --now vox.service && rm ~/.config/systemd/user/vox.service && systemctl --user daemon-reload` recipe. Linux-only; macOS (no `systemctl --user`) is gated out. (3) Unit tests lock in the cleanup sequence (exact subprocess argv, `check=False` on both `systemctl` calls, file removal, return value), the platform gate, the scope guard against touching `voxd`/`sudo`/`/etc/systemd`, and the install-time ordering (`cleanup_stale_user_unit → systemd_stop → ensure_port_free`). Doctor-side tests cover stale-subcommand-fails, current-subcommand-passes, file-absent-passes, non-Linux-skipped, unparseable-ExecStart-fails. Closes vox-45r.

## [4.3.0] - 2026-04-09

### Added

- **`vox daemon restart` subcommand**: cycle the running `voxd` daemon via the service manager without hand-running `systemctl` or `launchctl`. Refuses to run as root (sudo is invoked internally for the two service-manager calls only), detects macOS vs Linux, drives `_launchd_stop`/`_systemd_stop` + `_ensure_port_free` for a clean shutdown, starts the daemon via `sudo systemctl start voxd` or `sudo launchctl load -w ... && sudo launchctl kickstart -k system/com.punt-labs.voxd`, and polls the authenticated health endpoint (5s window, 200ms interval) until the new process is confirmed up. On success, prints the new pid and port. On failure, exits 1 and points at `~/.punt-labs/vox/logs/voxd.log`. This is the intended command after `uv tool upgrade punt-vox` — a plain upgrade replaces the wheel but leaves the long-running daemon untouched, so changes to daemon behavior do not take effect until the service is cycled.
- **Per-call provider API key on `vox unmute`, four input paths**: scope a single synthesis call to a specific provider API key, forwarded to `voxd` over the local WebSocket and injected into the provider's environment for the duration of one synthesize request. Motivation is single-user multi-key billing attribution — one user holding multiple ElevenLabs or OpenAI keys for separate billing projects, not multi-tenant isolation (vox remains a single-user tool). The key is never persisted to `keys.env`, never written to logs, never echoed to stdout (including `--json` mode), and never visible to concurrent requests on the same daemon. Four input paths are supported and are mutually exclusive:
  1. **`VOX_API_KEY` env var** (recommended for scripting): typer reads it natively via `envvar="VOX_API_KEY"`. On Linux, `/proc/<pid>/environ` is owner-only; macOS differs (see README), but env vars are still materially harder to snoop than argv.
  2. **`--api-key-file <path>`** (recommended for stored keys): reads the key from a file, strips trailing whitespace/newlines. Rejects missing paths and empty files via `typer.BadParameter`. Warns (but does not fail) when the file is world-readable (mode `& 0o004`), suggesting `chmod 600`. Intended layout: `~/.config/vox/key_<project>.txt` at mode 0600.
  3. **`--api-key-stdin`** (recommended for password managers): reads one line from stdin, strips whitespace. Refuses to read from a tty so a forgotten pipe fails loudly. Intended usage: `pass show vox/proj | vox unmute ... --api-key-stdin`.
  4. **`--api-key <value>`** (kept for back-compat and demo): direct CLI flag. Still accepted, but now emits a stderr warning whenever the value came from argv (distinguished from `VOX_API_KEY` via `ctx.get_parameter_source("api_key") == ParameterSource.COMMANDLINE`). The warning text points at the three safer paths. The env-var path does not warn. Empty strings are rejected via `typer.BadParameter` rather than silently falling back to the `keys.env` default.

  Prompted by Cursor Automation security review of PR #175, which flagged `--api-key` on the command line as a practical credential disclosure path via `ps`, `/proc/*/cmdline`, shell history, and terminal recordings — a real concern for the exact billing-isolation scripting scenario the feature targets. Closes vox-a3e (`voxd` already parsed `api_key` from the WebSocket message; this adds the CLI surface, safer input paths, and the end-to-end integration test that exercises the full chain).

### Changed

- **`vox doctor` reports daemon version and warns on wheel/daemon mismatch**: doctor now reads the running daemon's version from the authenticated WebSocket health payload and compares it against `importlib.metadata.version("punt-vox")` — the wheel installed on disk. Matching versions produce the existing green checkmark with the version appended (`✓ Daemon: running on port 8421 (provider: elevenlabs, version 4.2.0)`). Mismatched versions produce a yellow `⚠ Daemon: running on port 8421 (version 4.1.1 — wheel has 4.2.0, run 'vox daemon restart' to refresh)`. The refresh hint intentionally omits `sudo`: `vox daemon restart` refuses to run as root and invokes sudo internally only for the service-manager calls that require it, so a literal copy-paste of the hint works as your normal user. Exit code stays 0 — the daemon is still functional, just out of date — but the warning counter increments and the `--json` payload carries a `warned` field for machine consumption. Pre-version daemons (pre-commit 2 builds that lack `daemon_version` in their health response) fall back to the existing PASS message so older daemons do not falsely trip the warning.
- **voxd health WebSocket response includes `daemon_version` and `pid`**: the authenticated full health payload (WebSocket handler, not the unauthenticated HTTP `/health` route) now carries `daemon_version` from `importlib.metadata.version("punt-vox")` and `pid` from `os.getpid()`. Both are cached or computed at startup — no per-request metadata scan. The unauthenticated HTTP `/health` route is deliberately unchanged: leaking a running version to anonymous callers is a fingerprinting aid for targeted exploitation, and `pid` is a diagnostic-only detail. `vox doctor` uses `daemon_version` for the mismatch warning above; `vox daemon restart` uses `pid` to confirm the daemon came back up as a fresh process.

### Fixed

- **`unmute` and `chime` now return after enqueueing for playback, not after playback (vox-ehf)**: `VoxClient.synthesize()` and `VoxClient.chime()` previously waited for voxd's `"done"` message, which arrives only after the audio finishes playing. On long texts with slow ElevenLabs synthesis the combined synthesis + playback duration exceeded the 30-second `_TIMEOUT_SYNTHESIS` budget, causing `/recap` timeouts. Both methods now return when voxd sends `"playing"` (audio synthesized and queued), letting playback continue independently in voxd's queue worker. Dedup short-circuits (which send `"done"` directly with no `"playing"`) continue to terminate correctly. voxd's `_handle_synthesize` and `_handle_chime` now suppress the stale `"done"` send that fires after a client has already closed the connection on `"playing"`. Closes vox-ehf.

- **Stale voxd survived release-day smoke tests (vox-nmb)**: during the v4.2.0 end-to-end verification of the `--once` flag, a stale voxd daemon that had been running since 2026-04-07 — 20 hours before the new code was merged — silently accepted every new `once` field in synthesize messages, ignored it, and played every request. `vox doctor` reported "Daemon: running" because it only checked reachability, not version alignment between the wheel on disk and the running process. The stale daemon was caught only when a human noticed dedup was not working at all. Fix has three parts: (1) `vox daemon restart` subcommand so the correct upgrade flow is discoverable, (2) `daemon_version` in the health payload so doctor has something to compare against, (3) doctor warning on wheel/daemon mismatch so smoke tests fail loudly instead of producing a false-positive pass. Closes vox-nmb.
- **Per-call API key passthrough had no CLI surface and no integration test (vox-a3e)**: voxd has known how to parse `api_key` from the synthesize WebSocket message since PR #152 and already knew how to inject it into `os.environ[ELEVENLABS_API_KEY]` / `os.environ[OPENAI_API_KEY]` under `_env_lock` for the duration of one request. But no CLI flag exposed the capability, and no integration test verified the end-to-end flow, so the feature was effectively dead code. Adds `vox unmute --api-key` (see Added above) and a new `TestApiKeyPassthroughIntegration` suite in `tests/test_voxd.py` that drives the real Starlette app via `starlette.testclient.TestClient`, opens a real WebSocket, sends record messages with different keys, and verifies that a stub provider sees exactly the key the caller sent, with no cross-call leakage, and that the ambient environment is restored after each call. Four scenarios: single-call key, two sequential calls with different keys (the billing-isolation invariant), `api_key=None` fallback to the ambient environment, and ambient-key restoration after a per-call override. Closes vox-a3e.
- **Per-call `api_key` now bypasses the synthesis cache (CodeQL `py/weak-sensitive-data-hashing`)**: an earlier draft of this PR added `api_key` to the cache-key digest (first as MD5, then as SHA-256) to prevent billing-scope collisions on cache hits. CodeQL correctly flagged the SHA-256 variant under `py/weak-sensitive-data-hashing`: the taint analysis classifies `api_key` as password-class material, and any regular cryptographic hash (MD5, SHA-1, SHA-256, SHA-384, SHA-512, SHA-3, even HMAC variants) is inappropriate for hashing that material. The rule wants a password KDF (Argon2, scrypt, bcrypt, PBKDF2 with high iteration counts), none of which are acceptable for a cache-filename computation — Argon2 alone would add >100 ms per call. Arguing with the linter is a losing battle. The principled fix is **cache bypass**: the `api_key` parameter is removed from `cache.py` entirely, and `voxd._synthesize_to_file` now gates both the `cache_get` lookup and the `cache_put` store on `api_key is None`. Per-call billing scopes synthesize every time; anonymous calls keep the unchanged MD5 cache that is byte-identical to pre-v4.2.1 so existing on-disk entries remain reachable after upgrade. The mixed-scope caching the earlier draft allowed was a latent correctness hazard regardless: a per-call billing scope that accepts cached bytes from another scope is violating the whole point of the isolation. Scripts that want cache hits for repeated quips should use `keys.env` (the anonymous path); scripts that want billing attribution should accept that every call re-synthesizes. As a byproduct of adding the anti-poison test, a latent bug in `_handle_record` was also fixed — the record handler unconditionally `unlink`ed the path returned from `_synthesize_to_file`, which silently deleted the cache entry on every anonymous cache hit. The handler now only unlinks tempfiles (paths that are NOT inside `CACHE_DIR`).

## [4.2.0] - 2026-04-08

### Added

- **`vox unmute --once <seconds>`**: new CLI flag that forwards a per-call dedup TTL to voxd. When set, voxd will skip a synthesize+play of the same text if an identical text was already played within the window. The motivating use case is `biff wall` broadcasts: N Claude Code sessions in the same repo independently shell out to `vox unmute` on the same broadcast text, and the user should hear the announcement exactly once. Without the flag, identical requests play every time — there is no default dedup for speech. The flag takes a positive integer (seconds); the biff wall integration passes `--once 600` for a 10-minute window that comfortably covers cross-session delivery jitter. Closes vox-0e9 on the vox side. The biff side follows in a separate PR against `punt-labs/biff` coordinated on the biff message channel.

### Changed

- **Speech dedup is now opt-in via `once`**: the legacy `AudioDedup` class unconditionally deduped every synthesize request within a 5-second window, keyed on `(text, voice, provider)`. The always-on behavior was removed — callers that want dedup must set the `once: <ttl_seconds>` field on the synthesize/direct_play WebSocket message (or pass `--once <seconds>` on the CLI). Without `once`, every request plays. The new `OnceDedup` class replaces the old one with a per-call TTL, a hash keyed on `md5(text)` only (so identical text with different voices or providers collapses), and an observable `DedupHit` result so callers can log "wall skipped, already played 53s ago". `ChimeDedup` (renamed from the old `AudioDedup` for the chime path) keeps the existing 5-second always-on behavior — chimes are event markers and always deduped, speech is not. Breaking change for any caller that relied on the silent always-on dedup for identical speech; in practice the hook handlers (`vox hook signal/notification/stop`) do not produce cross-session or rapid intra-session duplicates, so this change is safe for them.
- **`VoxClient.synthesize()` returns `SynthesizeResult`**: the async and sync client `synthesize()` methods previously returned a bare `str` request id. They now return a `SynthesizeResult` dataclass carrying `request_id: str`, `deduped: bool`, `original_played_at: float | None`, and `ttl_seconds_remaining: float | None`. Existing callers that only read the request id update to `.request_id`; new callers can check `.deduped` to surface observable dedup status. Internal callers (`server.py`, `__main__.py`, `hooks.py`) updated.

- **ElevenLabs default model reverted to `eleven_v3`**: The previous default `eleven_flash_v2_5` was chosen for low latency and lower cost, but `eleven_v3` is the only ElevenLabs model that interprets bracket-style expressive tags (`[excited]`, `[weary]`, `[sighs]`) — which the `/vibe` feature is built around. Using a non-expressive default silently broke `/vibe` for every user who never set `TTS_MODEL` explicitly: tags were prepended to the synthesis text and rendered as the literal words "excited", "weary", "sighs" instead of as performance cues. Reverting to `eleven_v3` makes the headline `/vibe` feature work out of the box. Users who want the lower cost or latency of `eleven_flash_v2_5` can still override via `TTS_MODEL=eleven_flash_v2_5`. The deeper fix (vibe tag stripping for non-expressive providers/models) is documented under Fixed below.

### Fixed

- **Vibe tags spoken as literal words on non-expressive models (vox-fhl)**: when `voxd` synthesized text with `vibe_tags` set, it unconditionally prepended the tag string to the normalized text on both the synthesize and direct-play paths without checking whether the active provider+model interprets bracket-style tags as performance cues. Any provider or model that does not interpret tags (Polly, OpenAI, macOS `say`, Linux `espeak-ng`, and every ElevenLabs model except `eleven_v3`) spoke the literal words — `[serious]` was read aloud as "serious", `[weary]` as "weary", and so on. The capability information already existed on the `TTSProvider` protocol as `supports_expressive_tags`; the gating just was not consulted at the prepend site, and `apply_vibe` in `resolve.py` had the right shape but was dead code in production (defined and tested, never imported by any synthesis path). A new `ElevenLabsProvider.model_supports_expressive_tags` classmethod does a pure lookup against `_EXPRESSIVE_MODELS = {"eleven_v3"}` without instantiating the SDK. A new `split_leading_expressive_tags(text) -> (tags, body)` helper in `resolve.py` pulls leading bracket tags off the raw input before normalization — crucial because `normalize_for_speech` strips brackets as part of its punctuation pass, so any tag fed in after normalization has already been converted to a literal word. Both voxd synthesis paths now call a shared `_apply_vibe_for_synthesis(raw_text, vibe_tags, provider, model)` helper that (a) splits leading tags from raw text, (b) normalizes only the body, (c) re-attaches vibe tags only when the active provider+model supports them, dropping them entirely otherwise. Lazy ElevenLabs import inside the helper keeps espeak-only and say-only systems from pulling the ElevenLabs SDK at voxd module load. Regression tests exercise the full production call path (`split → normalize → gate`) instead of the helper in isolation, so future changes to the order of operations fail loudly. Closes vox-fhl. (#170)
- **Watcher notification consumer throttled on first event on fresh systems (vox-2sj)**: the consumer closure returned by `make_notification_consumer()` in `src/punt_vox/watcher.py` used a throttle check of the form `last = last_fired.get(event.signal, 0.0); if now - last < throttle_seconds: return` with `now = time.monotonic()`. On Linux, `time.monotonic()` returns `CLOCK_MONOTONIC` — seconds since boot. On freshly-booted CI runners, that value is small (typically 5-30 seconds at test start). With the default sentinel of `0.0` and a throttle window of 100 s, the first event for any signal computed `now - 0.0 ≈ 10`, `10 < 100`, throttle fired, and the consumer returned without calling `_announce_voice`. Mock call counts ended up at 0 instead of 1 and the test failed. The bug was invisible on macOS and persistent Linux dev boxes because their `monotonic()` values are in the thousands or millions (uptime in hours, days, or months), so the throttle never fired on the first call — it only manifested on GitHub Actions ephemeral Ubuntu runners where uptime at test start is smaller than the throttle window. A 2026-02-28 workaround in PR #45 had added `pytest.mark.skipif(CI=true)` decorators on the two failing tests; the decorators hid the tests from CI for five weeks on the incorrect hypothesis that Ubuntu CI read `config.notify` differently (the regex in `config.py` is platform-independent, so that hypothesis was provably false). Fixed by changing the sentinel from `0.0` to `None` and gating the throttle check on `last is not None`. New regression test `test_first_call_fires_when_monotonic_below_throttle_window` mocks `time.monotonic` to return `5.0` and asserts the first event still fires, reproducing the CI condition deterministically on any host. The `skipif` decorators were removed in the same PR so both tests now run on every platform. Closes vox-2sj. (#168)

## [4.1.1] - 2026-04-07

### Documentation

- **README setup walkthrough for cloud providers**: added a `Configure providers` section between Quick Start and Features. Covers acquiring API keys (ElevenLabs, OpenAI, AWS Polly) with signup and free-tier details, editing `~/.punt-labs/vox/keys.env` with a normal editor (no sudo), restarting the daemon via `systemctl`/`launchctl` to apply changes, and verifying with `vox doctor` + `vox unmute`. AWS Polly section documents both the `AWS_PROFILE` path (recommended for users who already use the AWS CLI) and raw `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` credentials. Slimmed the Environment Variables section to cross-reference Configure providers instead of duplicating the edit/restart instructions.

### Fixed

- **`vox daemon install` no longer requires `sudo` for everything**: install runs as the user. Per-user state under `~/.punt-labs/vox/` is created with normal user permissions — no chown, no fchown, no symlink defenses. Sudo escalation is scoped to three subprocess calls per platform on a fresh install (place the unit/plist via `install(1)`, register with the service manager, start the daemon), growing to four on macOS and five on Linux when upgrading a previously-installed service (the extra calls stop the old daemon via the service manager so launchd's `KeepAlive=true` and systemd's `Restart=on-failure` cannot respawn it mid-upgrade). Eliminates the entire class of symlink-attack and chown-ordering bugs that existed when the entire install ran as root inside a user-controlled directory. See DES-029 for the full rationale.
- **`vox daemon install` upgrades left the old voxd running with stale ExecStart**: the first iteration of the new install path used `systemctl enable --now voxd` on Linux and `launchctl load` on macOS. Both are no-ops when the service is already running, so on upgrade from an older install the previous voxd kept its stale binary/args baked in until the next reboot. Fixed by switching Linux to `systemctl enable` + `systemctl restart` (unconditional cycle) and adding `launchctl kickstart -k system/com.punt-labs.voxd` after `load` on macOS. Cursor Bugbot 3048294138 / Copilot 3048295072 on PR #162.
- **`vox daemon install` upgrade raced against the service manager**: `install()` called `_ensure_port_free` (which issues a direct `os.kill(SIGTERM)` to the stale voxd PID) before running the platform-specific install path. On macOS, launchd's `KeepAlive=true` immediately respawned the killed daemon with the OLD plist; on Linux, systemd's `Restart=on-failure` treated the kill as a failure exit and restarted the process under the old unit. By the time the new unit write + restart sequence ran, the service manager had already resurrected the old binary. Fixed by adding `_launchd_stop()` and `_systemd_stop()` pre-flight helpers that tell the service manager to stop the daemon (via `sudo launchctl unload -w` / `sudo systemctl stop voxd`) BEFORE `_ensure_port_free` runs. Both are idempotent — fresh installs with no prior unit file skip the sudo call entirely. The Linux sudo count goes from 4 to 5; the macOS sudo count stays at 4 because the redundant inline `unload -w` inside `_launchd_install` was removed (the pre-flight stop now owns the unload). Cursor Bugbot 3048416720 on PR #162.
- **`vox daemon install` silently broke when run under `sudo`**: the new install path runs as the invoking user, but offered no guard against a user who ran `sudo vox daemon install` out of habit. In that case `getpass.getuser()` returned `root`, `Path.home()` resolved to `/root`, all state landed under `/root/.punt-labs/vox/`, and the generated systemd unit had `User=root` so the daemon lost audio device access. `install()` now refuses to run with `os.geteuid() == 0` and emits a clear error directing the user to re-run without sudo. Copilot 3048295090 on PR #162.
- **`vox daemon install` crashed when an existing `keys.env` was unreadable**: `_write_keys_env` called `keys_path.read_text()` with no error handling, so a corrupted file (non-UTF-8 bytes, permission denied, not-a-regular-file) would abort the whole install with a stack trace. The read is now wrapped in a `try`/`except (OSError, UnicodeDecodeError)` that logs a warning and overwrites the file fresh from env values at install time. Copilot 3048295101 on PR #162.
- **`keys.env` was world-readable for a brief window during install**: `_write_keys_env` used `Path.write_text` + `Path.chmod(0o600)`, which creates the file via `open(..., "w")` — that produces mode `0o666 & ~umask` (typically `0o644` on a `0022` umask) and only chmods afterward. The file was world-readable for the few instructions between create and chmod, a real API-key exposure window. Fixed by opening the file with `os.open(..., O_WRONLY|O_CREAT|O_TRUNC, 0o600)` so the mode is set at create time. A post-write `os.chmod(0o600)` remains as belt-and-suspenders for unusual umasks. Copilot 3048402515 on PR #162.
- **`_write_keys_env` did not tighten the parent state dir permissions**: if the state dir was pre-created at an umask-widened mode (for example 0755 from an older version of vox, or by hand), a secrets file inside a world-traversable directory let other local users read the dir listing and mount further attacks. `_write_keys_env` now calls `parent.mkdir(mode=0o700)` and `parent.chmod(0o700)` on every invocation so the helper is self-contained regardless of whether `install()` has already run `ensure_user_dirs`. Copilot 3048402424 on PR #162.
- **`_voxd_exec_args` accepted non-executable files and directories**: `Path.exists()` returns True for directories, symlink loops, and non-executable regular files, so a broken `voxd` at `sys.executable.parent` would pass validation and get baked into `ExecStart=` — the service would then fail at runtime with an opaque systemd error. Fixed by probing `Path.is_file()` + `os.access(..., os.X_OK)` and raising `SystemExit` with a clear message before the install proceeds. Copilot 3048402463 on PR #162.
- **README contradicted itself on sudo-free key management**: the doc said "you never need sudo again to manage your keys" but then instructed users to run `sudo systemctl restart voxd` / `sudo launchctl kickstart` to apply key changes. Both statements were true in different senses (editing `keys.env` is sudo-free; restarting the daemon requires sudo for `systemctl`/`launchctl`), but the phrasing blurred them together. Rewrote the section to separate "edit the file — no sudo" from "restart the daemon to apply — requires sudo" with an explicit heading on the restart step. Copilot 3048402487 on PR #162.
- **voxd state dirs left at loose permissions on upgrade**: `_configure_logging` and `_read_or_create_token` created `~/.punt-labs/vox/{logs,run}` with `Path.mkdir(exist_ok=True)`, which respects the process umask — on most shells (umask `0022`) pre-existing directories stayed at `0755` and could leak spoken-text logs, auth tokens, and cached synthesis output to other local users. `voxd.main()` now calls `paths.ensure_user_dirs()` at startup, which chmods every subdir (`logs`, `run`, `cache`, root) to `0700` regardless of prior mode. Copilot finding 3048101870 on PR #162.
- **voxd state location regression**: PR #130 (the v3 architecture rewrite) moved per-user voxd state from `~/.punt-labs/vox/` to FHS system paths (`/etc/vox/`, `/var/log/vox/`, `/var/run/vox/`). This stranded existing users' API keys silently on upgrade and required sudo to edit personal API tokens. State is now back in `~/.punt-labs/vox/`. Users who had cloud provider keys configured before v3 (commit 49879af) will see them work again automatically — the keys were never deleted, voxd just stopped reading from the right location.
- **Stale `voxd` binary in systemd unit**: `daemon install` resolved `voxd` via `shutil.which()` and could bake a stale binary from an earlier `uv tool install` into the systemd `ExecStart=`. Now resolves from `Path(sys.executable).parent / "voxd"` so the unit always runs the same distribution that provides `vox`.

### Changed

- **Dead cross-user path helpers removed from `punt_vox.paths`**: `user_state_dir_for(user)` and `installing_user()` existed to support the old sudo-aware resolution (mapping `$SUDO_USER` to the target home dir at install time). Now that install runs as the invoking user, both helpers are dead code and have been deleted along with their tests. Cursor Bugbot 3048294140 on PR #162.
- **Path helpers extracted to `punt_vox.paths`**: voxd, service, and client all share one source of truth for per-user state paths. Removed the duplicated `_data_root()`/`_config_dir()`/`_log_dir()`/`_run_dir()` helpers from those modules. The new module is stdlib-only so both the heavy voxd import chain and the minimal client can depend on it.
- **systemd unit no longer sets `RuntimeDirectory=vox`**: runtime state lives in `$HOME/.punt-labs/vox/run/` now, so systemd does not need to create `/run/vox`.
- **State directories tightened to mode 0700**: `~/.punt-labs/vox/` and all four subdirectories (`logs`, `run`, `cache`, root) now use mode 0700, same policy as `~/.ssh` and `~/.gnupg`. Previously `logs/` and `cache/` inherited the process umask and could be world-readable on systems with a permissive default, which would leak spoken text, operational details, and cached synthesis output to other local users.

### Security

- **Smaller privileged surface in `vox daemon install`**: the install command no longer escalates to root for any per-user file operation. By running the entire per-user portion as the invoking user, the attacker-controlled `~/.punt-labs/` tree is written under normal kernel permission checks and the privileged code path shrinks to three `sudo` subprocess calls per platform on a fresh install (place the service file via `install(1)`, register with the service manager, start the daemon), growing to four on macOS and five on Linux when upgrading a previously-installed service. The extra upgrade calls are a pre-flight stop through the service manager so launchd's `KeepAlive=true` and systemd's `Restart=on-failure` cannot respawn the old daemon mid-upgrade. Eliminates the whole class of symlink/TOCTOU/chown-ordering attacks that required defensive code (`O_NOFOLLOW`, `O_EXCL`, `lchown`, `fchown`, parent-symlink rejection, fd-based fstat verification) in the old root-inside-$HOME design. `install()` also refuses to run with `os.geteuid() == 0` so the sudo-habit user gets an explicit error instead of silently landing state under `/root/`. Keys.env still rejects control characters (`\n`, `\r`, `\x00`) in provider values to prevent env-var smuggling — that's input sanitization, not a privilege defense, and still applies.

## [4.1.0] - 2026-04-06

### Added

- **Direct-play path for local TTS providers**: `espeak-ng` and macOS `say` now implement an optional `DirectPlayProvider` protocol, spawning their binary without the `-w`/`-o` flag and playing straight to the default audio device -- the same syscall and audio session a user's shell would use. Cloud providers (ElevenLabs, OpenAI, Polly) keep using the existing synthesize-cache-enqueue pipeline, so MP3 caching and dedup replay still work. This eliminates the WAV -> ffmpeg -> MP3 -> ffplay round-trip for local synthesis and removes an entire class of audio-session negotiation bugs on Linux. Direct-play and queued playback share a single `_playback_mutex`, so two concurrent clients can never produce overlapping audio.

### Fixed

- **voxd playback observability**: playback was fire-and-forget with player stderr piped to `DEVNULL`, making silent failures impossible to diagnose remotely. `_play_audio` now captures the spawn command, audio env vars at call time (`XDG_RUNTIME_DIR`, `PULSE_SERVER`, `DBUS_SESSION_BUS_ADDRESS`, etc.), exit code, elapsed wall time, file size, and full stderr (truncated to 2 KB with head + tail kept). Logs `ERROR` on non-zero exit or spawn failure, `WARNING` on suspicious sub-50ms "success", `INFO` with stderr summary on normal success. Voxd startup logs its full process environment (pid, uid, gid, cwd, binary, audio env) so operators can verify systemd env injection without poking at `/proc`. Synthesis now fails fast on 0-byte output -- the broken file is deleted, the cache is not poisoned, and the client gets an error response. The token-authenticated `health` WebSocket message exposes `audio_env`, `player_binary`, and `last_playback` so `vox doctor` surfaces playback state; the unauthenticated HTTP `/health` route returns only the minimal public status fields and never leaks environment variables or player stderr.
- **Silent playback on Linux**: voxd systemd unit lacked `XDG_RUNTIME_DIR`, so ffplay couldn't reach PulseAudio/PipeWire for audio output. Now captures audio session env vars at install time. Also adds `RuntimeDirectory=vox` so `/var/run/vox` is created automatically at service start.

## [4.0.3] - 2026-04-06

### Fixed

- **espeak-ng VoiceNotFoundError on Linux**: espeak provider crashed with `VoiceNotFoundError: en` on systems where espeak-ng only has qualified voice variants (`en-us`, `en-gb`) but no bare `en`. Voice resolution now registers bare ISO 639-1 fallback keys and `default_voice` discovers what's actually installed instead of assuming a hardcoded voice exists. Same fix applied to the macOS `say` provider for `samantha`.

## [4.0.2] - 2026-04-02

### Fixed

- **Symbol mispronunciation**: parentheses, brackets, and other non-speech symbols are now stripped before TTS synthesis — only prosody punctuation (`.` `,` `?` `!` `:` `;`) is preserved (#150)
- **Linux install failure**: `sudo vox daemon install` wrote root-owned `__pycache__` into user's uv tools directory, causing subsequent `uv tool install` to fail with Permission denied. Fixed with `PYTHONDONTWRITEBYTECODE=1` and cleanup step (#149)

### Changed

- Add `punt-labs/team` git submodule at `.punt-labs/ethos/` for agent definitions and identity data (#149)

## [4.0.1] - 2026-04-01

### Fixed

- **Stop hook hang**: fire-and-forget chime in Stop hook prevents 5s+ hang when voxd is slow or unreachable (#143)
- **Acronym mispronunciation**: TTS engines no longer pronounce OCR as "ocker" or MCP as "mick-pee" — ALL_CAPS acronyms (2-5 chars) are letter-spaced unless in a ~280-entry pronounceable-words allowlist (#144)
- **State persistence**: notify/speak/vibe session state now persists to disk, surviving MCP server restart (#142)

### Security

- Bump Pygments to 2.20.0 in lockfile — ReDoS CVE (#141)
- Bump punt-lux to 0.15.1, fastmcp to 3.2.0+ in lockfile — CVE-2026-32871, CVE-2026-27124 (#139)
- Bump PyJWT in lockfile — security fix (#138)

### Changed

- Track `.envrc` in version control; user overrides go in `.envrc.local` (gitignored) (#140)
- Add Skill() allow entries via punt auto settings (#137)

## [4.0.0] - 2026-03-29

## [3.0.0] - 2026-03-29

### Changed

- **BREAKING:** New `voxd` audio server daemon replaces the old `daemon.py`. Pure audio server — synthesizes text and plays through speakers. Knows nothing about MCP, hooks, projects, or Claude Code.
- **BREAKING:** System-level service install. macOS: `/Library/LaunchDaemons/` (sudo required). Linux: `/etc/systemd/system/` (sudo required). Daemon data in Homebrew prefix (macOS) or FHS paths (Linux), not `~/.punt-labs/vox/`.
- **BREAKING:** `mcp-proxy` eliminated. MCP server runs as direct stdio process (`vox mcp`). Plugin.json simplified.
- MCP server is now a thin client of `voxd`. Session state in memory, not `.vox/config.md`. No provider imports — cold start target < 500ms.
- Hook handlers call `voxd` via WebSocket client for audio instead of in-process synthesis.
- WebSocket protocol between clients and `voxd` — streaming-capable for future real-time voice.

### Added

- `voxd` binary entry point (`punt_vox.voxd:main`) — audio daemon with playback queue, dedup, caching.
- `punt_vox.client` — lightweight WebSocket client library (`VoxClient` async, `VoxClientSync` sync wrapper).

### Removed

- `daemon.py` — replaced by `voxd.py`
- `proxy.py` — mcp-proxy eliminated
- `ephemeral.py` — no project-directory writes from daemon
- `_config_path_override` ContextVar — daemon has no session/config concept
- PID-based CWD resolution via `lsof` / `/proc` — eliminated
- `playback.py` flock/pending/subprocess queue — daemon owns playback

## [2.0.0] - 2026-03-29

### Changed

- **BREAKING:** Data directory migrated from `~/.punt-vox/` to `~/.punt-labs/vox/` per org filesystem standard. Clean break — old directory is not read or migrated. Re-run `vox daemon install` after upgrade.
- Auth token is now stable across daemon restarts. Generated once at install time, persisted to `serve.token`, reused on daemon startup. Enables mcp-proxy reconnection without session restart.
- Daemon service config uses `vox` shim path (via `shutil.which`) instead of `sys.executable`. Survives venv recreation by uv.
- `TTS_MODEL` now persisted to `keys.env` alongside provider API keys.
- AWS credential check (`_has_aws_credentials()`) no longer cached with `lru_cache` — expired session tokens are detected correctly in long-running daemon.
- Hook scripts log errors to `~/.punt-labs/vox/logs/hook-errors.log` instead of `/dev/null`.
- `install.sh` now installs `mcp-proxy` after daemon setup for fast hook relay.
- Plugin MCP server command uses `-s` (non-empty) file checks instead of `-f` for token/port files.

### Fixed

- `vox daemon install` now unloads the existing launchd plist before loading the new one, preventing `launchctl load` I/O errors on upgrades. Same fix for systemd: stops the service before writing the new unit.
- `vox daemon install` now creates parent directory before writing token file, fixing `FileNotFoundError` on fresh installs.
- `vox daemon install` reuses existing auth token instead of always generating a new one, preventing session breakage during upgrades.
- Daemon validates auth token on startup — empty or unreadable token files produce actionable `SystemExit` messages instead of silent auth bypass.
- Hook logging initialized via `configure_logging()` in CLI hook entry point.
- `httpx` logger suppressed (noisy at INFO from OpenAI SDK).
- `install.sh` mcp-proxy step uses `python` instead of `python3` for `uv tool run` (python3 not guaranteed by `uv python install`).

## [1.11.0] - 2026-03-28

### Added

- Daemon provider key resolution via `~/.punt-vox/keys.env` — `vox daemon install` snapshots API keys (ELEVENLABS_API_KEY, OPENAI_API_KEY, AWS_*) from the caller's environment into a chmod 0600 config file; the daemon loads it at startup before provider auto-detection
- `install.sh` now runs `vox daemon install` as part of installation, with graceful fallback

## [1.10.3] - 2026-03-28

### Fixed

- Daemon identity check (`_is_vox_daemon_process`) now matches all invocation patterns: `punt_vox` (underscore), `punt-vox` (hyphen in uv tool path), and bare `vox serve` binary
- Daemon launchd plist and systemd unit now embed the user's `PATH` at install time so ffmpeg and other tools in `/opt/homebrew/bin` or `/usr/local/bin` are found
- `python -m punt_vox` now works — added missing `if __name__ == "__main__"` guard to `__main__.py`, which caused the launchd-launched daemon to exit silently

## [1.10.2] - 2026-03-28

### Fixed

- Chime audio now plays in daemon and installed modes — assets are bundled inside the Python package (`src/punt_vox/assets/`) so `_resolve_assets_dir()` resolves correctly when `CLAUDE_PLUGIN_ROOT` is not set
- `vox daemon uninstall` now kills the running daemon process instead of only removing the launchd/systemd config
- `vox daemon install` detects and kills stale daemon processes occupying the port before installing

## [1.10.1] - 2026-03-20

### Changed

- Session-start hook now auto-allows Skill permissions (`Skill(unmute)`, `Skill(mute)`, `Skill(recap)`, `Skill(vibe)`, `Skill(vox)`) alongside MCP tool globs, matching the beadle PLUGIN_RULES pattern
- Session-start hook JSON output uses `jq -n --arg` instead of raw heredoc interpolation, preventing malformed JSON from special characters in action messages
- Legacy MCP pattern removal now cleans up temp files on failure

### Removed

- `commands/ask-test-dev.md` — dev test artifact for AskUserQuestion; finding documented in DES-022

## [1.10.0] - 2026-03-14

### Added

- Daemon mode (`vox serve`): single long-running process serving MCP-over-WebSocket and hook relay, fronted by mcp-proxy for sub-10ms session startup and hook dispatch
- Audio deduplication: prevents duplicate playback when multiple sessions receive the same notification (e.g. biff wall)
- Service management (`vox daemon install/uninstall/status`): registers launchd (macOS) or systemd (Linux) service for auto-start at login
- mcp-proxy integration: plugin.json falls back to `vox mcp` (stdio) when mcp-proxy is unavailable
- Hook scripts use daemon relay (~15ms) with subprocess fallback (~500ms)
- `vox install` now installs mcp-proxy binary and registers daemon service
- `vox doctor` checks mcp-proxy and daemon status

## [1.9.1] - 2026-03-13

### Fixed

- Flaky hook tests: tests that mock `subprocess.run` now also mock `cache_get` to prevent cache hits from bypassing the mocked call path
- README install.sh SHA checksum was stale after v1.9.0 release (57334a4→40c3769)

## [1.9.0] - 2026-03-13

### Added

- MP3 caching for quip phrases: hook speech is cached by (text, voice, provider) in `~/.punt-vox/cache/`, eliminating redundant TTS API calls and reducing latency on repeated quips
- `vox cache status` and `vox cache clear` CLI commands for cache management
- Text normalization for natural speech: snake_case, camelCase, and programmer abbreviations (stderr, stdout, eof, etc.) are converted to spoken English before synthesis

## [1.8.0] - 2026-03-12

### Added

- `/unmute` voice picker: shows AskUserQuestion dialog with featured voices when no argument is given (providers with 2+ voices only)
- `/ask-test-dev` command for testing AskUserQuestion inside command execution

### Changed

- Hook output: `speak` with voice shows "sarah at the mic" instead of generic "voice on"
- Hook output: `who` shows "25 agents standing by" instead of "25 voices checked in"

### Fixed

- Strip leading expressive tags (e.g. `[serious]`) from text when the provider does not support them, preventing literal bracket words in speech
- Prune `vibe_signals` to the most recent 20 entries, preventing unbounded growth in long sessions

## [1.7.0] - 2026-03-12

### Added

- `show_vox` MCP tool to display status widget in Lux display window (notifications, voice, vibe, engine)
- `applet.py` module for Lux element tree construction and display server connection
- `punt-lux` as optional dependency (`uv add punt-vox[lux]`)

## [1.6.0] - 2026-03-10

## [1.5.0] - 2026-03-10

### Added

- Technical architecture specification (`docs/architecture.tex`) — 15-page
  LaTeX document covering provider architecture, audio pipeline, hook
  integration, security model, and known limitations
- Chime mappings and voice phrases for `git-commit` and `pr-created` signals
- `All checks passed` pattern to lint-pass signal detection

### Fixed

- MCP server now uses worktree-safe `resolve_config_path()` instead of
  hardcoded CWD-relative path — voice mode no longer silently fails in
  git worktrees
- Signal classification unified: watcher delegates to
  `hooks.classify_signal()` instead of maintaining a separate pattern table
- `tests-pass` pattern tightened from bare `passed` to `[0-9]+ passed` to
  prevent false positives on prose text

### Removed

- `remove_ephemeral_dir()` from `ephemeral.py` — dead code that would have
  destroyed session config via `shutil.rmtree(.vox/)`
- Dead `voice_enabled` field from `VoxConfig` and `ALLOWED_CONFIG_KEYS`

## [1.4.1] - 2026-03-10

### Fixed

- **Hook stdin hang** — `_read_hook_input()` used blocking
  `sys.stdin.read()` which hangs when Claude Code does not close the
  pipe. Replaced with non-blocking `os.read()` in a `select` loop.
  Also removed unnecessary stdin drain calls from 5 handlers that
  never used the data. See DES-027.

## [1.4.0] - 2026-03-09

### Added

- **Continuous mode hooks**: UserPromptSubmit acknowledgment, SubagentStart/Stop announcements, SessionEnd farewell speech — all fire only in continuous mode (`/vox c`) except SessionEnd which fires whenever notify != off
- Centralized quip registry (`quips.py`) for all hook speech phrases — localization and theming ready
- Shared `_speak_phrase()` helper eliminates duplication across continuous-mode hook handlers

## [1.3.0] - 2026-03-09

## [1.2.4] - 2026-03-08

### Changed

- Switch default ElevenLabs model from `eleven_v3` to `eleven_flash_v2_5` (~75ms latency, 40k char limit)
- Correct `eleven_v3` per-request character limit to 5,000

### Added

- Mid-session model switching via `/vox model <name>` (shorthands: `v3`, `flash`, `turbo`, `multilingual`)
- Mid-session provider switching via `/vox provider <name>` (`elevenlabs`, `openai`, `polly`, `say`, `espeak`)
- `provider` and `model` fields in `.vox/config.md` session config
- Expressive tags (`[excited]`, `[warm]`, etc.) are now model-aware — only applied on `eleven_v3`

### Fixed

- Config model no longer leaks across incompatible providers (e.g. ElevenLabs model passed to OpenAI)

## [1.2.3] - 2026-03-08

### Fixed

- `vox install` used wrong plugin ID (`tts@punt-labs` instead of `vox@punt-labs`)
- `install.sh` pinned to stale version 1.2.0 instead of current release

## [1.2.2] - 2026-03-08

### Added

- `/mute` replies with a random personality phrase instead of silent confirmation (#83)
- PreCompact hook plays a playful "be right back" message before context compaction in continuous mode (#83)
- Distinct `"compact"` chime signal for PreCompact (distinguishable from task-complete `"done"`)

### Fixed

- Global `--json` flag now placed before subcommand for correct typer parsing in hook subprocess calls
- PreCompact CLI command drains stdin to prevent pipe backpressure
- Z spec partition test coverage for notify/speak state machine (#82)

## [1.2.1] - 2026-03-06

## [1.2.1] - 2026-03-07

### Fixed

- Clean stop hook reason — no internal data leak (#75)
- Align CLI with punt-kit standards (#76)
- Address Copilot review feedback from PR #76 (#77)
- Remove `[skip ci]` from release-plugin.sh (suppressed tag-triggered releases)

### Changed

- Add Makefile per makefile.md standard (#78)

## [1.2.0] - 2026-03-05

### Added

- Per-segment `vibe_tags` in `unmute` and `record` — each segment can now specify its own expressive tags, overriding the session default (same pattern as `voice` and `language`)

### Changed

- `unmute` MCP tool is now non-blocking — synthesis and playback run in a background thread; tool returns predicted metadata immediately
- `record` MCP tool is now non-blocking — returns the predicted file path immediately; synthesis completes in background
- CLI `status` now includes `vibe_tags` and `vibe_signals` in both text and `--json` output (parity with MCP `status` tool)

## [1.1.1] - 2026-03-05

### Fixed

- suppress-output hook now formats `notify`, `speak`, and `status` MCP tool output as compact panel lines instead of dumping raw JSON

## [1.1.0] - 2026-03-05

### Added

- `notify` MCP tool: set notification mode (y/n/c) and session voice without Bash
- `speak` MCP tool: toggle spoken notifications (y/n) without Bash
- `status` MCP tool: query current vox state (provider, voice, notify, vibe) without Bash

### Changed

- All slash commands (`/vox`, `/unmute`, `/mute`, `/vibe`) now use MCP tools exclusively — no Bash, Read, or Write tool permissions needed (DES-017)
- Stop hook block reason now embeds vibe context (mode, mood, signals) so the model never needs to Read `.vox/config.md`

## [1.0.3] - 2026-03-05

### Fixed

- SessionStart hook now updates stale commands on plugin upgrade — previously deployed commands in `~/.claude/commands/` were never refreshed, leaving users with old allowed-tools, old MCP tool names, and prompt-driven logic instead of CLI calls

## [1.0.2] - 2026-03-05

### Fixed

- Provider auto-detection now prefers AWS Polly over macOS `say` and Linux `espeak` when AWS credentials are valid — Polly produces significantly better audio than local fallbacks
- Session voice gracefully falls back to provider default when the configured voice isn't available in the current provider (e.g. ElevenLabs voice "sarah" stored in config but Polly is the active provider)
- Suppressed elevenlabs SDK pydantic v1 `UserWarning` on Python 3.14+ — their upstream issue, filtered at runtime and in test config

## [1.0.1] - 2026-03-05

### Changed

- CLI: replaced `vox on`/`vox off`/`vox mute` with `vox notify y|n|c`, `vox speak y|n`, and `vox voice <name>` — aligns CLI with the standard that every slash command has a corresponding CLI command
- Slash commands `/vox`, `/unmute`, `/mute` now delegate to CLI via Bash instead of manually editing `.vox/config.md` with Read/Edit tools
- Slash commands no longer request `Edit`, `Read`, or `Write` tool permissions — only `Bash` and MCP tools

## [1.0.0] - 2026-03-05

### Fixed

- `/unmute` now sets `notify: "c"` (continuous mode) so spoken notifications actually fire — previously only set `speak: "y"` while `notify` defaulted to `"n"`, silently disabling all notifications
- `/mute` now specifies exact config file location and create-if-missing behavior, matching other config-writing commands
- Hooks resolve `.vox/config.md` via `git rev-parse --git-common-dir` so config is found from worktrees
- All config-writing commands now specify exact `.vox/config.md` location and create-if-missing behavior, preventing agents from searching other directories
- Config path resolution now catches `subprocess.TimeoutExpired` (was `TimeoutError`, which subprocess.run never raises)
- Hook chime resolution uses `CLAUDE_PLUGIN_ROOT` env var for asset paths, fixing chime playback for pip-installed packages where `__file__` resolves into site-packages
- `notify-permission.sh` called non-existent `vox synthesize` — now uses `vox unmute` via the Python hook dispatcher
- Signal classifier now checks lint patterns before test patterns, fixing false matches where "Found N errors" was classified as `tests-fail` instead of `lint-fail`
- Signal classifier uses `re.MULTILINE` so `^` anchors match line starts in multi-line bash output (matches original bash `grep` behavior)
- Chime filename resolution normalizes signal hyphens to underscores (`tests-pass` → `chime_tests_pass.mp3`)
- Checkmark pattern in signal classifier uses literal `✓` instead of raw `\u2713` which was never interpreted as Unicode

### Changed

- Merged `/vox-on` and `/vox-off` into a single `/vox` slash command with `y` (enable), `n` (disable), or `c` (continuous) argument
- `/vox y` and `/vox c` now preserve existing `speak` setting on subsequent calls; only first init defaults `speak` to `"y"`
- Migrated hook business logic from bash to Python via `vox hook <event>` CLI dispatcher — stop, post-bash, and notification hooks are now thin shell gates delegating to testable pure functions in `hooks.py`
- Deleted `hooks/state.sh` — all config reading, mood classification, chime resolution, and audio helpers now use their Python equivalents

## [0.11.0] - 2026-03-04

### Added

- **Mic API**: new MCP server key `mic` with four tools: `unmute` (synthesize + play), `record` (synthesize + save), `vibe` (session mood), `who` (voice roster)
- Both `unmute` and `record` accept a uniform `segments` list — callers no longer need different tools for different cardinalities
- CLI product commands: `vox unmute`, `vox record`, `vox vibe`, `vox on`/`off`, `vox mute`, `vox version`, `vox status`
- Slash commands: `/unmute [@voice]`, `/mute`, `/vox on`, `/vox off`
- Shared modules: `resolve.py` (voice/language/output resolution, vibe application), `voices.py` (blurbs, excuses), `config.py` write API
- Vibe-driven chime notifications: chimes now reflect session mood (bright/neutral/dark) via pitch-shifted variants (±3 semitones)
- Mood classification module (`mood.py`): maps free-form vibe strings to bright/neutral/dark tonal families
- Mood-aware chime resolution: `resolve_chime_path(signal, mood=)` with four-level fallback chain
- Per-signal chime assets: distinct sounds for tests-pass, tests-fail, lint-pass, lint-fail, git-push-ok, and merge-conflict (chime mode only)
- Signal-aware `resolve_chime_path(signal)` with automatic fallback to `chime_done.mp3`
- Generation script `scripts/generate_chimes.py` for reproducible chime synthesis with mood variants

### Changed

- MCP server key from `vox` to `mic` (`plugin:vox:mic`)
- Hook matchers updated from `_vox__` to `_mic__` patterns
- Session-start hook auto-migrates old permission patterns and cleans retired commands

### Removed

- MCP tools: `speak`, `chorus`, `duet`, `ensemble`, `set_config`, `list_voices`
- CLI commands: `synthesize`, `synthesize-batch`, `synthesize-pair`, `synthesize-pair-batch`
- Slash commands: `/say`, `/speak`, `/notify`, `/voice`

## [0.10.1] - 2026-03-03

### Fixed

- Plugin name on release tags is now `vox` (was `vox-dev` — release script was not run before v0.10.0 tag)

## [0.10.0] - 2026-03-03

First release as **punt-vox**. The PyPI package name changed from `punt-tts` to `punt-vox`, the CLI binary changed from `tts` to `vox`, and all internal paths and namespaces follow suit. No functional changes — this is a pure rename release.

### Changed

- Rename env var `TTS_OUTPUT_DIR` → `VOX_OUTPUT_DIR`
- Rename default output dir `~/tts-output` → `~/vox-output`
- Rename ephemeral dir `.tts/` → `.vox/` (config, audio)
- Rename log/state dir `~/.punt-tts/` → `~/.punt-vox/` (logs, playback lock, pending queue)
- Release workflow installs `punt-vox` and verifies `vox --help` (was `punt-tts`/`tts`)
- `install.sh` installs `punt-vox` package with `vox` binary (was `punt-tts`/`tts`)
- Rename plugin name `tts-dev`/`tts` → `vox-dev`/`vox` (plugin.json, hooks, commands, settings)
- Plugin MCP tool namespace `mcp__plugin_tts_vox__*` → `mcp__plugin_vox_vox__*`
- Session-start hook cleans up all legacy `mcp__plugin_tts*` permission patterns
- Hook scripts use `vox` CLI binary (was `tts`)
- All documentation updated: README.md, CLAUDE.md, DESIGN.md, prfaq.tex — `punt-tts`/`tts` → `punt-vox`/`vox`

### Fixed

- `release-plugin.sh` no longer fails when no `-dev` commands exist — name swap proceeds with a warning instead of aborting
- `restore-dev-plugin.sh` no longer fails when `.claude/commands/` directory doesn't exist at the release commit
- PostToolUse hook matcher now fires for standalone MCP server registrations (`mcp__vox__*`) in addition to plugin-namespaced names

## [0.9.0] - 2026-02-28

### Added

- `list_voices` MCP tool: browse available voices for the current provider with curated personality blurbs, shuffled featured list (capped at 6), and full voice roster
- `/voice` bare invocation: displays featured voices with blurbs and prompts user to pick with `/voice <name>`
- `list_voices` panel handler in suppress-output hook: displays voice count or "here's who's around"

### Fixed

- Permission notification hook now uses the active session voice instead of always defaulting to matilda

## [0.8.1] - 2026-02-28

### Fixed

- eSpeak provider now passes language codes (e.g. `en-us`) to `espeak-ng -v` instead of display names (e.g. `English_(America)`) which espeak-ng rejects
- Installer uses `doctor || true` so diagnostic failures don't abort the install script under `set -eu`

## [0.8.0] - 2026-02-28

### Added

- Per-session voice selection: `/voice <name>` sets a default voice for all speak/chorus/duet/ensemble calls. Stored in `.tts/config.md` as `voice` field. Use `/voice clear` to revert to provider default.
- Session event watcher: daemon thread in MCP server tails the session transcript and announces milestones (tests passed, lint clean, code pushed) in real-time when `notify=c`. Uses pattern matching on bash tool output, per-signal throttle, and voice/chime modes.
- `vibe_tags` parameter on `speak` and `chorus` tools: applies expressive tags and clears `vibe_signals` in one step, replacing the separate `set_config` call in the stop-hook flow.
- Friendly voice-not-found errors: when a voice can't be resolved, providers raise `VoiceNotFoundError` with structured data. MCP tool handlers catch it and return a playful message (e.g. "bob stepped out for a coffee. How about matilda, aria, charlie?") instead of a raw traceback.

### Fixed

- Audio no longer clips at the end: 150ms trailing silence appended to all output files (single, stitched, and batch) to prevent MP3 frame truncation.

### Changed

- macOS Say provider default voice changed from Fred to Samantha.
- Stop hook now signal-gated: only fires when `vibe_signals` is non-empty (real work happened). Prevents empty recaps after trivial commands like `/say hello`.
- Signal accumulation decoupled from `vibe_mode`: signals accumulate in all modes (auto, manual, off), not just auto. Required for stop hook gating.
- New signal types: `git-commit` (commit created) and `pr-created` (pull request opened).

## [0.7.1] - 2026-02-27

### Added

- Batch `set_config` mode: pass `updates` dict to write multiple config fields in a single atomic read-write ([#33](https://github.com/punt-labs/vox/pull/33))

### Fixed

- Vibe tags (`[excited]`, `[weary]`, etc.) are now only prepended when the provider supports expressive tags (ElevenLabs). Other providers (Polly, OpenAI, say, espeak) no longer speak bracketed tag text literally ([#39](https://github.com/punt-labs/vox/pull/39))
- Speak hook output now uses gendered pronouns: "matilda said her piece" instead of "matilda said the piece" ([#31](https://github.com/punt-labs/vox/pull/31))
- `install.sh` now uses uninstall+install instead of `claude plugin update` which did not reliably pick up new versions
- `tts doctor` MCP check now says "Claude Desktop MCP" (not "MCP server") and suggests the correct command (`tts install-desktop`)

## [0.7.0] - 2026-02-27

### Added

- macOS `say` command fallback provider: zero-config, offline TTS when no API keys are configured. Uses Fred voice (the iconic 1984 Mac voice) to nudge users toward configuring a real provider
- Linux `espeak-ng` fallback provider: zero-config, offline TTS using espeak-ng speech synthesizer. Auto-detected when espeak-ng is installed and no API keys are set
- Auto-detection now falls back to system TTS on both platforms: `say` on macOS, `espeak` on Linux (when installed). Final fallback remains `polly`
- `--provider say` and `--provider espeak` flags for explicit use of system voices
- Installer and `tts doctor` now check for espeak-ng on Linux and show install hints when no API keys are configured

## [0.6.1] - 2026-02-27

### Fixed

- Stop hook no longer leaks `vibe_mode` debug data in user-visible hook error display; vibe data now read from `.tts/config.md` via Read tool ([#26](https://github.com/punt-labs/vox/pull/26))
- `_apply_vibe()` skips vibe tag prepend when text already starts with an expression tag, preventing `[calm] [calm] ...` doubling ([#27](https://github.com/punt-labs/vox/pull/27))
- Voice name now appears in speak tool panel output; `suppress-output.sh` unwraps the `{"result": "..."}` wrapper Claude Code adds to MCP tool responses
- `install.sh` now detects already-installed plugin and runs `claude plugin update` instead of silently no-oping ([#28](https://github.com/punt-labs/vox/pull/28))
- TestPyPI verification in release CI uses `--refresh` to bust stale uv index cache between retry attempts ([#29](https://github.com/punt-labs/vox/pull/29))

## [0.6.0] - 2026-02-27

### Added

- `/vibe` command with three modes: `auto` (default — detects session mood from signals), `manual` (`/vibe <mood>` — user-specified), and `off`
- Auto-vibe: PostToolUse hook on Bash accumulates session signals (test pass/fail, lint, git ops) and stop-hook continuation passes them to Claude for expressive tag selection
- `set_config` MCP tool: writes plugin config fields atomically, replacing Read/Write/Edit file-tool pattern for all config mutations
- Panel display for vibe shifts: `♪ vibe shifted to [weary]` on config writes
- Shell linting with shellcheck in quality gates and CI

### Changed

- `/vibe`, `/notify`, `/speak`, `/voice` commands now use `set_config` MCP tool instead of Read/Write/Edit file tools
- Panel output personifies the voice: `♪ matilda has spoken` instead of `♪ spoken — matilda (elevenlabs)`. Provider name dropped from display. Phrase pool adds variety.

## [0.5.0] - 2026-02-27

### Fixed

- Installer now refreshes marketplace clone before plugin install, ensuring existing users get the correct `source.ref` pins

## [0.4.0] - 2026-02-27

### Added

- Dev/prod namespace isolation for plugin testing (`claude --plugin-dir .`)
- Audio playback serialized via `flock` — concurrent utterances queue instead of overlapping or being killed
- `tts play <file>` CLI command for flock-serialized audio playback (used by hooks)
- Cross-platform audio player: `afplay` (macOS) with `ffplay` (Linux/cross-platform) fallback

### Changed

- MCP server key renamed from `tts` to `vox`; tools now appear as `plugin:tts:vox` (was `plugin:tts:tts`)
- Installer and session-start hook clean up legacy `mcp__plugin_tts_tts__*` permission entries from pre-rename installs

## [0.3.6] - 2026-02-26

### Changed

- ElevenLabs provider now uses streaming API (`text_to_speech.stream()`) for lower time-to-first-audio

### Fixed

- Installer now runs `claude plugin update` when plugin is already installed; previously users stayed stuck on old versions
- Chime playback detached from hook process group (`nohup + disown`) so audio survives hook exit
- `--scope user` added to `claude plugin update` command (Bugbot catch)

## [0.3.5] - 2026-02-26

### Changed

- Config moved from global `~/.claude/tts.local.md` to per-project `.tts/config.md`; settings no longer leak across projects

### Added

- `argument-hint` frontmatter on `/notify`, `/speak`, `/say`, `/voice` commands for autocomplete-style picker hints

## [0.3.4] - 2026-02-26

### Fixed

- Plugin name is now `tts` on main (was `tts-dev`); marketplace installs show `plugin:tts:tts` instead of `plugin:tts-dev:tts`
- MCP server command is now `tts serve` (was `uv run tts serve`); works on machines without `uv`
- Install script handles SSH auth failure with HTTPS fallback ([#8](https://github.com/punt-labs/vox/issues/8))

### Removed

- Dev/prod plugin name swap scripts (`release-plugin.sh`, `restore-dev-plugin.sh`); plugin.json is always prod-ready

## [0.3.3] - 2026-02-25

### Added

- MCP server `instructions` field primes stop hook behavior at session start (prior-context delivery pattern from biff)

### Changed

- Stop hook phrases now imply the action naturally ("Speaking my thoughts...", "Saying my piece...") — playful for the user, instructive for the model

## [0.3.2] - 2026-02-25

### Changed

- Stop hook reason now shows playful, randomized phrases ("Clearing my throat...", "Finding my words...") instead of functional text

## [0.3.1] - 2026-02-25

### Fixed

- MCP tools now return valid JSON instead of Python repr (fixes panel display showing raw dict)
- Stop hook reason is a clean single line instead of a wall of instructions
- CLI no longer crashes on `tts install` when no TTS provider API keys are configured (lazy provider init)

## [0.3.0] - 2026-02-25

### Changed

- Renamed MCP tools: `synthesize` -> `speak`, `synthesize_batch` -> `chorus`, `synthesize_pair` -> `duet`, `synthesize_pair_batch` -> `ensemble`
- Tool panel output now shows voice and provider context with `♪` prefix (two-channel display pattern)
- Permission and idle notification phrases now vary randomly instead of repeating the same line

## [0.2.0] - 2026-02-25

### Added

- `/notify` command: toggle task-completion and permission-prompt notifications (y/c/n)
- `/speak` command: toggle voice vs chime-only notifications (y/n)
- `/recap` command: on-demand spoken summary of Claude's last response
- Stop hook: blocks Claude's stop to generate a spoken summary when /notify is enabled
- Notification hook: async audio alerts for permission prompts and idle prompts
- Chime audio: bundled MP3 tones for `/speak n` mode (task-complete and needs-approval)
- Shared hook state library (`hooks/state.sh`) for reading tts.local.md from bash
- `tts install`: marketplace-based installation via `claude plugin install tts@punt-labs`
- `tts uninstall`: full cleanup (plugin, commands, permissions, marketplace)
- Marketplace installer module (`installer.py`) for punt-labs marketplace registration
- Release scripts: `scripts/release-plugin.sh` and `scripts/restore-dev-plugin.sh`
- DESIGN.md: design decision log for notification architecture

### Changed

- `tts install` now installs via the Claude Code marketplace (previously wrote Claude Desktop config)
- Old Claude Desktop install behavior moved to `tts install-desktop`
- Plugin uses `uv run` in dev mode for working tree source exercise

## [0.1.0] - 2026-02-25

### Added

- Multi-provider TTS engine extracted from langlearn-tts
- ElevenLabs, AWS Polly, and OpenAI TTS providers
- CLI commands: synthesize, batch, pair, pair-batch, doctor, install, serve
- MCP server with synthesize, batch, pair, and pair-batch tools
- Ephemeral output mode (`.tts/` directory in cwd) for transient audio
- Claude Code plugin shell: plugin.json, hooks, /voice and /say commands
- Auto-detection: ElevenLabs > Polly (based on available API keys)
- GitHub Actions CI: lint and test workflows
