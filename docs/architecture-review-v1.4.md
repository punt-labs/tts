# Architecture Review: Vox v1.4.0

> **Point-in-time artifact (noted 2026-07-03).** This review was conducted against the v1.4.0 architecture (March 2026) and its findings are mostly marked Resolved. It does **not** reflect the shipped v4.9.0 system (daemon/client split, LaunchAgent service model, config split, music/remote subsystems). Two findings remain open — F9 (config-write race) and F12 (voice-list filtering). Superseded by the v4.9.0 `architecture.tex` refresh; retained as a historical record.

Reviewed against `docs/architecture.tex` (March 2026).

## Summary

The implementation is substantially faithful to the spec. 13 findings:
3 spec gaps (undocumented components), 2 spec inaccuracies, 1 dead config field,
1 worktree correctness bug, 1 concurrent write risk, and several undocumented
behavioral details.

---

## Findings

### F1: `watcher.py` is entirely absent from the spec

**Classification:** Spec gap — significant undocumented component

The spec's Component Map (Section 1.2) lists 14 modules. The implementation has 15.
`src/punt_vox/watcher.py` (498 lines) implements `SessionWatcher`, `derive_session_dir`,
`make_notification_consumer`, `classify_output`, and chime path resolution. It is launched
by `server.py:_start_watcher()` at line 662, runs as a daemon thread, and tails
`~/.claude/projects/{cwd}/` JSONL files to classify bash output in real time and fire
chimes/speech when `notify=c`.

Architecturally significant because:

- Creates a second signal-classification code path parallel to `hooks.py:classify_signal()`
  (see F2)
- Directly imports from `punt_vox.core`, `punt_vox.providers`, and `punt_vox.playback` —
  the only module other than the CLI/server that synthesizes audio autonomously
- Uses `CLAUDE_PLUGIN_ROOT` for asset resolution, duplicating logic from `hooks.py`
- Its `_announce_voice()` (line 232) fails silently (bare `except Exception` + log),
  extending the "Background Failure Opacity" limitation beyond what Section 8.4 documents

---

### F2: Signal classification has two divergent implementations

**Classification:** Architectural inconsistency

The spec Section 6.3 describes a single table of eight patterns. There are actually two
independent classifiers:

**`hooks.py:_SIGNAL_PATTERNS` (line 254)** — runs on PostToolUse hook events via `signal.sh`:

- `tests-pass`: `r"passed"`, `r"tests? ok"`, `"\u2713.*passed"` — matches the bare word "passed"
- `tests-fail`: `r"ERRORS?\b"` — word-boundary anchored
- `git-commit`: `r"^\[.+\] .+"` — bracket at line start

**`watcher.py:_PATTERNS` (line 50)** — runs on MCP server JSONL tailing:

- `tests-pass`: `r"\d+ passed"` — requires numeric prefix
- `tests-fail`: `r"\d+ errors? during"` — different pattern
- `lint-pass`: adds `r"All checks passed"` not in hooks.py
- No `git-commit`, `pr-created`, or `merge-conflict` patterns at all

This is undocumented divergence, not an intentional design split.

---

### F3: `voice_enabled` config field is dead state

**Classification:** Spec gap — orphan field

`config.py:VoxConfig` (line 64) includes `voice_enabled` (`"true"/"false"`), it is in
`ALLOWED_CONFIG_KEYS` (line 140), and defaults to `"true"` in `read_config()`. The spec's
Config File Format table (Section 5.2) documents nine fields — `voice_enabled` is not among
them. It is never consumed anywhere in `hooks.py`, `server.py`, `__main__.py`, or
`resolve.py`. Appears vestigial from an earlier `/voice toggle` design.

---

### F4: The spec incorrectly claims 8-character filenames

**Classification:** Spec inaccuracy

Section 4.1 states: "MD5 hash of text → 8-hex-char filename."

`types.py:generate_filename()` at line 296:
`digest = hashlib.md5(text.encode()).hexdigest()[:12]`

The digest is **12** hex characters, not 8.

---

### F5: `say` provider format incompatibility claim is stale

**Classification:** Spec claim incorrect

Sections 4.6 and 8.5 state the `say` provider "outputs LEF32 at 22,050 Hz" and is
incompatible with `stitch_audio()`.

`providers/say.py:synthesize()` (line 156) now:

1. Runs `say -o {aiff_path}` (produces AIFF)
2. Calls `ffmpeg` to convert AIFF → MP3 (lines 195–211)
3. Writes proper MP3 to `output_path`

The known limitation about format incompatibility is no longer accurate. Multi-segment
synthesis with `say` works when ffmpeg is present (which pydub also requires).

---

### F6: Stop hook guard on empty `vibe_signals` is undocumented

**Classification:** Spec gap — undocumented behavior

Section 2.2 step 6 says "In voice mode: selects a random phrase from the quip pool." It
does not mention that `handle_stop()` has an early return when `config.vibe_signals` is
empty (`hooks.py` lines 217–219). In practice, the stop hook produces no audio at all on
the first stop unless some bash commands have been run. This is a significant behavioral
gate not described in the spec's flow.

---

### F7: MCP server ignores `resolve_config_path()` — worktree bug

**Classification:** Architectural correctness issue — **highest priority**

The spec Section 5.2 states: "`resolve_config_path()` uses `git rev-parse --git-common-dir`
to locate the config file in worktree setups."

`server.py` line 72 defines `_CONFIG_PATH = Path(".vox/config.md")` as a module-level
constant. All MCP tool handlers use this hardcoded path, not `resolve_config_path()`.

The hooks correctly use `resolve_config_path()`. This means: in a worktree, the MCP server
writes to worktree-local `.vox/config.md` while hooks read from the main repo root's
`.vox/config.md`. Enabling voice via `/unmute` (MCP) sets `notify=y` in the wrong file,
and the stop hook (which reads from the git-common-dir) sees `notify=n`.

**Result:** Voice mode silently fails in worktrees.

---

### F8: `record` tool shares F7's worktree issue

**Classification:** Same root cause as F7

The `record` tool (server.py line 418) calls `get_provider()` without explicit config path.
`providers/__init__.py:get_provider()` (line 172) reads from `DEFAULT_CONFIG_PATH =
Path(".vox/config.md")`, not from `resolve_config_path()`. Provider and model session
overrides are invisible in worktrees.

---

### F9: Config writes have no file-level locking

**Classification:** Undocumented limitation

`write_fields()` in `config.py` performs read-then-write (lines 208–222) with no file lock.
The MCP server's background synthesis thread and `handle_post_bash` hooks can write
`vibe_signals` concurrently. Both paths do unguarded read-modify-write on the same file.
The race window is narrow but real in continuous mode with fast bash commands.

The playback lock (`playback.lock`) governs audio playback, not config writes.

---

### F10: `TTSProvider` protocol has 11 members, not 9

**Classification:** Spec incompleteness

The spec Section 3.1 lists 9 required members. `TTSProvider` inherits from `AudioProvider`
(`types.py` line 196), which defines `generate_audio` and `generate_audios` (lines 88–92).
All five providers implement these. The spec's table is short by two members.

The spec also states "No provider imports appear outside its own file." The providers import
`split_text` and `stitch_audio` from `punt_vox.core` (local imports inside methods). The
spec's intent (isolate SDK imports) is satisfied, but the phrasing is imprecise — it should
say "no provider SDK imports appear outside its own file."

---

### F11: Synthesis skip contradicts "No Audio Caching" claim

**Classification:** Spec inaccuracy

Section 7.1 states: "There is no audio caching. Every synthesis call hits the provider API."

`server.py:_synthesize_segments()` (lines 190–197) checks `if out_path.exists()` and skips
synthesis when a prior result file exists at the deterministic path. For `ephemeral=false`
calls or when ephemeral cleanup doesn't run, the same text will not re-synthesize.

---

### F12: `list_voices()` silently filters description suffixes

**Classification:** Undocumented behavior (minor)

`elevenlabs.py:list_voices()` (line 244) returns
`sorted(k for k in VOICES if " - " not in k)`. The voice cache contains both `"adam"`
and `"adam - dominant, firm"`. The filter silently excludes long-form names. Intentional
but undocumented.

---

### F13: OpenAI rate mapping is direct linear, not "inverse linear"

**Classification:** Spec inaccuracy (minor)

Section 3.3 states: "CLI percentage → OpenAI speed float (0.25–4.0, inverse linear)."

`providers/openai.py:_rate_to_speed()` line 201: `return max(0.25, min(4.0, rate / 100))`.
Rate 90 → speed 0.9; rate 110 → speed 1.1. This is **direct** linear mapping. "Inverse"
would mean rate=50 → speed=2.0.

---

## Priority Resolution Order

| Priority | Finding | Status |
|----------|---------|--------|
| P0 | F7, F8 | **Resolved** — `_CONFIG_PATH` replaced with `_config_path()` → `resolve_config_path()`; `get_provider()` accepts `config_path` |
| P1 | F2 | **Resolved** — watcher delegates to `hooks.classify_signal()`; pattern tightened (`[0-9]+ passed`) |
| P1 | F3 | **Resolved** — `voice_enabled` removed from `VoxConfig`, `ALLOWED_CONFIG_KEYS`, and all tests |
| P2 | F4 | **Resolved** — spec corrected to 12-hex-char |
| P2 | F5 | **Resolved** — spec updated: say outputs MP3 via ffmpeg, compatible with `stitch_audio()` |
| P2 | F13 | **Resolved** — spec corrected to direct linear |
| P2 | F11 | **Resolved** — spec notes synthesis-skip behavior |
| P2 | F1 | **Resolved** — watcher added to spec Component Map |
| P2 | F6 | **Resolved** — stop-hook guard documented in spec |
| P2 | F10 | **Resolved** — `generate_audio`/`generate_audios` added to spec protocol table |
| P3 | F9 | Open — concurrent write risk (narrow window, documented as known limitation) |
| P3 | F12 | Open — voice list filtering (minor, undocumented behavior) |

## File Reference Index

| Finding | File | Lines |
|---------|------|-------|
| F1 | `src/punt_vox/watcher.py` | 1–485 |
| F1 | `src/punt_vox/server.py` | 644–659 |
| F2 | `src/punt_vox/hooks.py` | 270–281 |
| F2 | `src/punt_vox/watcher.py` | 50–63 |
| F3 | `src/punt_vox/config.py` | 64, 107–109, 140 |
| F4 | `src/punt_vox/types.py` | 296 |
| F5 | `src/punt_vox/providers/say.py` | 156–212 |
| F6 | `src/punt_vox/hooks.py` | 217–219 |
| F7 | `src/punt_vox/server.py` | 72 |
| F9 | `src/punt_vox/config.py` | 187–224 |
| F10 | `src/punt_vox/types.py` | 84–92, 196 |
| F11 | `src/punt_vox/server.py` | 187–197 |
| F13 | `src/punt_vox/providers/openai.py` | 194–201 |
