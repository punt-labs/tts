# OO Refactoring Status

Last updated: 2026-07-03
Main: `9cb5d53` (post-#281). Package version **v4.9.0** (shipped 2026-07-03).

**Active, ongoing goal.** Detailed phase tracker for the OO refactor; kept in sync with the root `RESUME.md`. Phase 1 and Phase 2 Step 5 are complete; Phase 2 Steps 6–11 and most of Phase 3 remain.

## What Was Done

| PR | Title | Key changes |
|----|-------|-------------|
| #256 | OO tooling foundation | oo_score.py, OutputResolver, ruff rules |
| #257–#259 | voxd decomposition (Steps 0–10) | 2,729-line `voxd.py` + 20-field `DaemonContext` eliminated into config/chimes/dedup/playback/synthesis/health/router/daemon |
| #260 | service decomposition | `service/` package (5 classes), handler modules |
| #261 | SessionConfig + DoctorCheck | server.py refactor, doctor.py extraction |
| #262 | SynthesisSpec + ConfigStore + music package | types_synthesis.py, config.py, `voxd/music/` |
| #263 | server dedup + playback/synthesis cleanup | `_process_segments`, SessionConfig encapsulation, `play_audio` decomposition |
| #264 | Phase E/F/G + coupling + dead code | OutputFormatter, ApiKeyResolver, AudioMigration, DaemonRestarter; provider instance voice caches; 315 lines dead code removed; 3 coupling cycles broken |
| #265 | tooling | max-complexity gate → 10 |
| #267 | v3 Phase 1 Step 1 | CacheKey |
| #268 | coverage | resolve.py 50%→100% |
| #269 | v3 Phase 1 Steps 2–4 | PlaybackResult, MusicTrack, HookPayload |
| #270 | v3 Phase 2 Step 5 | Signal + SignalLog extracted from hooks.py |

## Current Metrics (main @ 9cb5d53)

The ratchet is green on touched files, but the codebase has not converged — 14 files remain over `module_size` 300, and two procedural hotspots still fail:

- **`__main__.py`** (~1,035 metric / 1,220 wc): `max_complexity`, `module_size`, low `method_ratio`.
- **`server.py`** (~850 metric / 1,030 wc): same failures.
- Also over threshold: `normalize.py`, `client.py` (`init_violations 1`, `classes_per_module 5`), `doctor.py`, `synthesis.py`, `playback.py`, `music/loop.py`.
- The git-diff ratchet permits adjacent-file drift; `installer.py` and `core.py` are pinned exactly at 300; last `--rebaseline` was #281.

## What Remains — v3 Execution Plan

Full plan: `oo-execution-plan-v3.md`. Hidden classes: `hidden-classes-analysis.md`.

### Phase 1: Value Objects (Steps 1–4) — ✅ COMPLETE

| Step | Class | Status |
|------|-------|--------|
| 1 | CacheKey | DONE (#267) |
| 2 | PlaybackResult | DONE (#269) |
| 3 | MusicTrack | DONE (#269) |
| 4 | HookPayload | DONE (#269) |

### Phase 2: Domain Objects (Steps 5–11) — 1 of 7 done

Dependencies: Step 6 depends on 5. Step 9 depends on 8. Step 11 depends on 8 and 9.

| Step | Class | Files | Status |
|------|-------|-------|--------|
| 5 | Signal + SignalLog | signal.py, hooks.py, config.py | **DONE (#270)** |
| 6 | Vibe | vibe.py, 5+ files. Data carrier only. | **NOT STARTED — next up** |
| 7 | Voice + VoiceResolver | voice.py, voices.py, resolve.py, server.py | NOT STARTED |
| 8 | SynthesisSpec behavior | types_synthesis.py, client.py, server.py, synthesis.py | NOT STARTED |
| 9 | Segment | segment.py, server.py, __main__.py | NOT STARTED |
| 10 | Notification | notification.py, hooks.py | NOT STARTED |
| 11 | Utterance | utterance.py, client.py, server.py, __main__.py, synthesis.py, speech_handlers.py | NOT STARTED |

### Phase 3: Complexity Reduction (Steps 12–17) — blocked on Phase 2

| Step | What | Status |
|------|------|--------|
| 12 | audio_migration CC reduction | NOT STARTED |
| 13 | server.py CC reduction | NOT STARTED (blocked on Steps 8–11) |
| 14 | __main__.py CC reduction | NOT STARTED (blocked on Steps 8–11) |
| 15 | chunked synthesis helper | DONE |
| 16 | delete music_handlers.py | DONE |
| 17 | `__all__` on every module | NOT STARTED — 50/77 files have it, **27 remain** |

## How to Resume

1. **Next: Phase 2 Step 6 (Vibe).** Step 5 (Signal) is merged and unblocks it.
2. Work Steps 6–11 (domain objects). Steps 13–14 (the `server.py`/`__main__.py` hotspots) are **dependency-blocked** on Steps 8–11 — do Phase 2 first and the complexity reduction falls out.
3. Step 17 (`__all__`) is independent; 27 files remain.
4. Delegate implementation to `rmh`; peer-review designs with `rej`. One mission = one task.

## Key Design Decisions (still governing)

- **Vibe is a data carrier only.** It does NOT absorb `apply_vibe_for_synthesis` (needs provider knowledge) or `resolve_tags` (belongs on SignalLog).
- **Utterance is the request, not the lifecycle.** Text + SynthesisSpec + request_id. Does NOT own output path, playback, or result.
- **No new packages.** Coupling analysis (`package-restructure-design.md`) concluded the flat layout is correct after the 3 coupling fixes; the real debt is hidden domain classes, not missing directories.
- **Dropped:** ProviderSelection (→ `SynthesisSpec.resolved()` factory), HealthStatus (DaemonHealth already returns the data), ConfigField (two frozensets + one function beats 9 dataclass instances).

## Reference Documents

| Document | What it contains |
|----------|-----------------|
| `oo-execution-plan-v3.md` | Complete 17-step plan with peer-review revisions |
| `hidden-classes-analysis.md` | 7 hidden domain classes (Signal ✅, Vibe, Voice, SynthesisSpec, Segment, Notification, Utterance) |
| `package-restructure-design.md` | Coupling analysis — why no new packages |
| `oo-design-assessment-v2.md` | Initial per-file assessment |
| `music-package-design.md` | Music subsystem redesign (done) |
| `oo-refactoring-plan.md` | Original 52-step plan (Steps 0–17 done) |
