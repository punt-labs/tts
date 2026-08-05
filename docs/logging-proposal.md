# Vox Logging Audit & Proposal

Status: The **P1 security items are IMPLEMENTED** (vox-q637, PR #337) and the **standard revision is drafted** (punt-kit #225). The **P2/P3 structural + observability items remain a proposal** (tracked as vox-fdmm). This document is the durable audit + the agreed plan, not a pending decision.
Date: 2026-07-17
Audits: rmh (config/rotation/multi-process/gaps/noise/readability), djb (security), verified on the running v4.12.3 by grepping `~/.punt-labs/vox/logs/` — not inferred from code.

Thesis (operator): **zero cases where we cannot tell what the system did (never guess), but not noisy — readable by a user, not a developer**, with configuration/rotation following PEP + Python + security best practice.

---

## 1. What exists today (verified sink map)

| File | Size | Rotation | Writer(s) | Mode |
|------|------|----------|-----------|------|
| `tts.log` | 4.2 MB + 5 | RotatingFileHandler 5MB×5 | MCP server, **every hook process**, CLI, detached playback — **multi-writer** | **0644** |
| `voxd.log` | 3.5 MB + 3 | RotatingFileHandler 5MB×5 | daemon only (single writer) | **0644** |
| `voxd-stderr.log` | **18 MB, unbounded** | none | launchd `StandardErrorPath` — **duplicates** voxd.log (78k INFO lines) | **0644** |
| `hook-errors.log` | 3.8 MB, unbounded | none | hook shell `2>>` — **duplicates** tts.log warnings | 0644 |
| `vibe-trace.log` | 62 KB | none | MCP server + hook, `O_APPEND` single-write | **0600** ✓ |

Two structural facts drive everything below:

1. **The daemon writes `voxd.log`; everything else writes `tts.log`.** An operator tailing one file is blind to half the system. Two files, **two duplicated `dictConfig` blocks** (`logging_config.py` + `voxd/config.py`) that will drift.
2. **`tts.log` is written by up to five process types, one short-lived process per hook event, concurrently across Claude sessions** — each installing its own `RotatingFileHandler`.

---

## 2. Findings

### 2a. Multi-process safety (HIGH, structural)

`RotatingFileHandler` is documented as **not multiprocess-safe**: `doRollover()` renames `tts.log → tts.log.1`; two processes crossing 5 MB near-simultaneously both rename, and a writer can hold an fd to a file renamed out from under it → lost/misfiled lines. The rotation files sit at exactly 5,242,xxx bytes — rollover fires routinely under multi-writer load, so the race window is hit regularly. `voxd.log` (single writer) and `vibe-trace.log` (atomic `O_APPEND`) are safe. **`vibe-trace.py` is the model to generalize.** QueueHandler/QueueListener does **not** fix this — queues don't cross process boundaries.

### 2b. Silent gaps — "must guess what happened" (the core ask)

1. **Program state machine has NO logger at all** (`programs/state.py`, `program.py`, `*_handler.py`, `producer.py`, `service.py` — `grep getLogger` returns nothing). The `off→generating→playing→rotating` machine the DESIGN and Z-spec model is invisible. A wrong transition — the bas7/#291 failure class the formal model exists to prevent — produces no log.
2. **Music-generation success is silent; only failure logs** (`fill_recorder.py:52` produces, logs nothing; `:68`/`filler.py:234` log failures). You can see music break but never confirm a track generated.
3. **Provider selection is invisible** (`providers/__init__.py:98-143`) — explicit vs config vs auto-detect, which won and why: unlogged. AWS-credential probe failure swallowed (`:156`). "Why did/didn't Polly get picked?" is unanswerable.
4. **Malformed hook payload swallowed** (`hooks.py:111`) — garbled payload indistinguishable from empty pipe.
5. **Hook "no config found" is silent** (`hooks.py:439/464/429`) — "hook never fired" indistinguishable from "fired, found no config, gave up."
6. **Auth rejection has zero record** (`voxd/router.py:60` closes bad-token WS with no log) — the one authn boundary; an unauthorized daemon connection is unprovable.
7. Split-brain playback (detached path → tts.log, daemon path → voxd.log); core `TTSClient.synthesize` (`core.py:141`) unlogged at its own layer.

### 2c. Noise — clutters a user-readable log

- **7 INFO lines per single chime** (router connect → chime → playback start → spawn(dumps full `audio_env` dict) → ok → done → disconnect). Live counts: playback 28,918, router 20,244.
- **Daemon double-write** — every daemon INFO duplicated into the 18 MB unbounded `voxd-stderr.log`.
- `frontmatter.py:115` logs 4 lines per vibe change (one per field); `mcp.server.lowlevel` library noise (`Processing request of type CallToolRequest`, 38×, no tool name) unsuppressed.

### 2d. Readability — INFO reads as debug output

Examples (message text): `cache MISS: id= provider=openai voice= size=566 chars_in=5 not cached` → **`synthesized 5 chars with openai, cached`**; `Session config: notify=c speak=n voice=roger provider=None vibe_mode=auto` → **`ready — voice roger, chimes only, auto vibe`**; `Direct-play ok: provider=say voice= elapsed=0.512s chars=12` → **`spoke 12 chars with say (0.5s)`**.

### 2e. Security

- **File perms: `tts.log`, `voxd.log`, and ALL rotated backups are 0644** (world/group-readable); only `vibe-trace.log` is 0600. `RotatingFileHandler` sets no mode and never re-tightens pre-existing files; backups inherit 0644. **The standard's own reference `configure_logging` produces 0644 — vox copied it verbatim.** Protection currently rests entirely on the `logs/` dir being 0700 (defense-in-depth already failed; `~/.punt-labs` is 0755).
- **Content leak (HIGH):** the music prompt is logged verbatim at INFO (`providers/elevenlabs_music.py:63`) into `voxd.log` + the 18 MB `voxd-stderr.log` — agent-authored content transmitted to ElevenLabs, which the standard says never to log.
- **Log injection:** standard paths do none of `vibe-trace`'s escaping. Client-controlled wire fields interpolate `%s` unescaped (`system_handlers.py:62`, voice/provider/`request_id` in `speech_handlers.py`/`synthesis.py`, ffmpeg stderr in `subprocess_player.py:69`).
- **Correctly handled (keep):** spoken text never logged (chars/hash/voice only); auth token never logged (path only, constant-time compare, uvicorn scrubbed); provider keys never logged (env allowlist).

---

## 3. Proposed vox changes (prioritized)

**P1** is security + structural, **P2** closes silent gaps, **P3** is noise + readability.

1. **[P1]** Harden all log files to **0600** (active + rotated backups + fchmod pre-existing), reusing the `private_state.py` model. Fixes the confirmed 0644.
2. **[P1]** **Log auth rejections durably** (`router.py`, metadata only — client addr, never the token).
3. **[P1]** **Make `tts.log` multi-writer-safe**: route client-side logging **through the daemon** over the existing WebSocket (one owner, one file — also collapses the two-files/two-configs split-brain), with a tiny `O_APPEND` fallback for the daemon-down case (what hooks log today). Alternative if consolidation is too big: replace the client `RotatingFileHandler` with the `vibe_trace` `O_APPEND` line-writer + external `logrotate`.
4. **[P1]** **Kill the daemon double-write** — drop the stderr handler / launchd `StandardErrorPath` (`voxd/config.py:269`, `launchd.py:112`). Removes 18 MB unbounded 0644 duplicate.
5. **[P1]** **Stop logging the music prompt verbatim** — INFO logs `len`+short hash; full prompt at DEBUG only (`elevenlabs_music.py:63`).
6. **[P1]** **Escape untrusted values** (`%r` or the shared `vibe_trace` escape table) on the standard log paths.
7. **[P2]** **Log program state transitions** — one logger in the state machine: `music: off→generating (celtic)`, `generating→playing part 3`, `playing→rotating`.
8. **[P2]** **Log music-generation success** symmetrically with failure (`fill_recorder.py:52`).
9. **[P2]** **Log provider selection + AWS-probe failure** (`providers/__init__.py`).
10. **[P2]** Turn silent hook no-ops (malformed payload, config-absent) into one traceable line each.
11. **[P3]** Collapse 7-lines-per-chime → 1; demote transport/counter/`audio_env` lines to DEBUG.
12. **[P3]** Suppress `mcp` library loggers; add a vox-owned per-tool INFO line (`mic:unmute — 5 chars`).
13. **[P3]** Rewrite user-facing INFO as plain sentences (§2d); unify the two configs into one module; add rotation to the append-only files.

---

## 4. Proposed standard revisions (`punt-kit/standards/logging.md`)

1. **Multi-process safety (new).** `RotatingFileHandler` is not multiprocess-safe. For a file written by >1 process: either a single owner process (consolidate) or atomic `O_APPEND` single-line appends (`vibe_trace` model); `RotatingFileHandler` only for single-writer files. Explicitly: QueueListener does not solve cross-process writing.
2. **File mode 0600 (new) + fix the reference.** Mandate log files created at 0600, pre-existing re-tightened via `fchmod` on open, and **rotated backups chmod'd to 0600** (`doRollover` hook). Correct the reference `configure_logging` (it produces 0644).
3. **Log injection (new).** Never `%s`-interpolate untrusted values (wire message, subprocess stderr, provider error body, file-derived); use `%r` or a control-char/newline/Unicode-line-sep escape.
4. **Correct the stderr sections.** stderr is discarded for MCP/hook processes under Claude Code; any security- or proof-relevant event MUST reach a durable file, never stderr alone. (vox-9po7 lesson.)
5. **Security events to log (new, bounded).** authn/authz outcomes (never the credential), rejected/malformed requests, permission-tightening failures → WARNING to the durable file. Diagnosis, not a SIEM: no alerting, no aggregation, metadata only.
6. **No-silent-gaps principle (sharpen "What Not to Log").** Log every **decision, state change, outcome (success AND failure, symmetrically), and boundary cross**; skip pure no-ops, per-iteration, and redundant cross-layer logs. Rule of thumb: *absence of a log line means nothing happened* — so anything that happens must leave one. This reconciles "don't log routine success" (a success already implied by a logged outcome downstream is redundant; a success that is the only evidence a thing occurred is required).
7. **User-readable INFO vs developer DEBUG (new).** INFO messages are plain-language "what happened" a user can follow — translate internal codes (`notify=c` → "chimes only"). Structured key=value dumps, transport bookkeeping, counters, and intermediate computation go to DEBUG.
8. **Document the sink map (new).** Each project states which processes write which file; prefer one durable file per tool; call out and avoid split-brain (operator tailing one file blind to another).

---

## 5. Recommendation & sequencing

Three rollback-coherent units, in order:

1. **Security fix (P1 #1,#2,#4,#5,#6) + standard §4.2/§4.3/§4.4/§4.5** — this is a security patch (world-readable logs, content leak, unprovable auth failures, injection). Ship first. djb worker, rmh/gvr eval.
2. **Standard revision** (§4 all) — I author `logging.md` changes against the merged audit; propagate the submodule ref after.
3. **Structural + observability feature (P1 #3, P2, P3)** — multi-writer consolidation, the silent-gap fills, noise/readability. This changes the logging architecture → design mission first (rmh design → leader review → impl). Biggest change; do it deliberately, not rushed.

Open decisions for the operator are listed in the accompanying message.
