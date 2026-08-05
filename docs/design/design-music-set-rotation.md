# Music Set Rotation Design

**Status:** DEFERRED — NOT IMPLEMENTED. As of v4.9.0, music plays a single vibe-driven track with gapless handoff on vibe change (`/music next` for a fresh track); set rotation is not built (`music_set_size`/`music_tracks` do not exist in the code). **Open question to resolve before implementation:** the default set size is inconsistent in this doc — the body (§7) sets `set_size = 10` (clamp 1–10) while the cost model (§8) assumes a default of 3 (~6,000 credits). At ~2,000 credits/track a default of 10 is ~20,000 credits per vibe; pick the intended default before building.

## Goal

Enhance `_music_loop` from single-track looping to per-vibe set rotation
with background fill. After initial generation the daemon plays from a
local library at zero credit cost, generating new tracks only when the
set is incomplete.

## Current State

`_music_loop` in `voxd.py` generates one track via
`ElevenLabsMusicProvider.generate_track()`, then loops it. Vibe changes
cancel any in-flight generation and start a new one while the old track
keeps playing (DES-033 gapless handoff). Tracks are saved to
`~/Music/vox/tracks/` as `{vibe}-{style}-{YYYYMMDD-HHMM}.mp3` (DES-035).
Music plays in a separate subprocess at reduced volume (DES-030). One
session owns the music at a time (DES-031).

## Design

### 1. Track Set State on DaemonContext

Three new flat fields on `DaemonContext`, following the existing pattern
of mutable state on the context object:

```python
self.music_tracks: list[Path] = []     # discovered + generated, in shuffle order
self.music_track_index: int = 0        # next track to play
self.music_set_size: int = 10          # default, configurable (per music_on)
```

Remove `self.music_track: Path | None` -- the track list replaces it.
Keep `music_track_name`, `music_replay`, `music_proc`, `music_state`,
`music_changed`, `music_vibe`, `music_style`, `music_owner`,
`music_mode` as-is.

No new dataclass. The context already carries `music_vibe` and
`music_style`; duplicating them in a separate object would create a
synchronization obligation for no benefit. A vibe change rebuilds the
track list from scratch, so the vibe and the track list are never out
of sync in a meaningful way.

### 2. Track Discovery

A new `_discover_tracks()` function scans `~/Music/vox/tracks/` for
files whose name starts with `{vibe_slug}-{style_slug}-`:

```python
def _discover_tracks(vibe: str, style: str) -> list[Path]:
    """Find existing tracks for this vibe+style combination."""
    output_dir = _music_output_dir()
    if not output_dir.is_dir():
        return []
    vibe_slug = _slugify(vibe, max_len=20) or "ambient"
    style_slug = _slugify(style, max_len=20) or "mix"
    prefix = f"{vibe_slug}-{style_slug}-"
    return sorted(
        p for p in output_dir.glob(f"{prefix}*.mp3")
        if p.is_file()
    )
```

`sorted()` produces lexicographic order, which is chronological because
filenames end with `YYYYMMDD-HHMM`. No `stat()` calls are needed.

This is compatible with `_auto_track_name()` which already produces
`{vibe}-{style}-{YYYYMMDD-HHMM}` filenames. Existing tracks from
single-track mode are discovered automatically.

### 3. Track Set Lifecycle in MusicLoop

When music turns on or the vibe changes:

1. Call `_discover_tracks(vibe, style)` to find cached tracks.
2. Shuffle the discovered list (`random.shuffle`).
3. Set `ctx.music_tracks = tracks`, `ctx.music_track_index = 0`.
4. If `len(ctx.music_tracks) > 0`, start playing immediately from
   index 0.
5. If `len(ctx.music_tracks) < ctx.music_set_size`, spawn a background
   generation task to fill the gap.

The `_music_loop` changes structurally:

```python
while music_mode == "on":
    ctx.music_tracks = _discover_and_shuffle(ctx)
    ctx.music_track_index = 0

    # Start playing immediately if we have any tracks
    if ctx.music_tracks:
        start playback of ctx.music_tracks[ctx.music_track_index]

    # Fill loop: generate missing tracks in background
    gen_task = None
    if len(ctx.music_tracks) < ctx.music_set_size:
        gen_task = create_task(_generate_music_track(ctx))

    # Playback rotation loop (same inner wait structure as today)
    while music_mode == "on":
        wait on: proc.wait(), music_changed.wait(), gen_task

        if proc ended naturally:
            # Advance to next track in set
            ctx.music_track_index = (ctx.music_track_index + 1) % len(ctx.music_tracks)
            break  # respawn with next track

        if gen_task completed successfully:
            new_track = gen_task.result()
            ctx.music_tracks.append(new_track)
            if len(ctx.music_tracks) < ctx.music_set_size:
                gen_task = create_task(_generate_music_track(ctx))
            else:
                gen_task = None  # set is full, stop generating

        if gen_task failed:
            # Same retry logic as today (backoff, max 3 attempts)
            # The current track keeps playing

        if music_changed (vibe change):
            # Cancel gen_task, rebuild track list for new vibe
            break to outer loop

        if music_mode == "off":
            kill everything, break
```

When `ctx.music_tracks` is empty (no cached tracks and first generation
is needed), the loop falls through to the generation-first path -- same
as the current `current_track is None` branch. This preserves the
existing gapless handoff guarantee from DES-033.

### 4. Background Fill Without Blocking Speech

No change needed. Music generation already runs in `asyncio.to_thread()`
inside `ElevenLabsMusicProvider.generate_track()`. Speech and chimes use
the separate `_playback_consumer` queue and `_playback_mutex`. Music
plays in its own subprocess at reduced volume (DES-030). The two paths
are already independent.

Background fill generates tracks sequentially — one at a time, not in
parallel. When a track completes, it is appended to the track list; if
the set is still incomplete, the next generation starts. This avoids
multiplying concurrent ElevenLabs API requests and keeps credit spend
predictable.

The only contention point is the ElevenLabs API key. Generation and
speech synthesis can run concurrently -- ElevenLabs allows multiple
concurrent requests per key. If rate-limiting becomes an issue, the
generation task will raise `ApiError` and the retry logic handles it.

### 5. Rotation Logic

Tracks rotate in shuffle order. When a track finishes playing (ffplay
exits), the index advances:
`ctx.music_track_index = (ctx.music_track_index + 1) % len(ctx.music_tracks)`.
When the index wraps around to 0, reshuffle the list (avoid repeating
the same order).

No track is ever deleted by the rotation. The set grows up to
`ctx.music_set_size` and then stays fixed. New generation stops once
the set is full.

### 6. Vibe Change Handling

When `music_changed` fires and the vibe has actually changed
(compare `(vibe, style)` against `ctx.music_vibe, ctx.music_style`):

1. Cancel any in-flight generation task.
2. Do NOT kill the current playback subprocess yet (gapless handoff).
3. Discover and shuffle tracks for the new vibe via `_discover_tracks()`.
4. Set `ctx.music_tracks`, `ctx.music_track_index = 0`.
5. If new track list has tracks: kill old subprocess, start first track.
6. If new track list is empty: start generation. Old track keeps playing
   until generation completes (existing DES-033 behavior).
7. Once the new track is ready or a cached track starts: spawn
   background fill if set is incomplete.

If the vibe changes again during background fill, the fill task is
cancelled and the process repeats for the newest vibe.

### 7. Set Size Configuration

`set_size` is passed via the `music_on` WebSocket message as an
optional integer field. It defaults to 10 if omitted.

```python
# In _handle_music_on:
set_size = int(msg.get("set_size", 10))
ctx.music_set_size = max(1, min(set_size, 10))  # clamp 1-10
```

The MCP server (`server.py`) and CLI (`__main__.py`) pass it through:

- CLI: `vox music on --set-size 5`
- MCP: `music_on` tool parameter `set_size`

No persistent config file for this -- it is per-session. The default
of 10 is a module-level constant `_DEFAULT_SET_SIZE = 10`.

### 8. Cost Model

- ~2000 credits per track (ElevenLabs music generation, 2-minute track)
- Default set of 3 = ~6000 credits per vibe
- After initial generation, replay is free (zero credits)
- Vibe change to a new vibe costs up to 6000 credits for 3 tracks
- Vibe change back to a previously-played vibe costs 0 credits
  (tracks are cached on disk)

### 9. Replay and music_replay Flag

The existing `music_replay` flag handles `/music play <name>` and
`/music on --name <name>`. This continues to work: when
`music_replay=True`, the loop uses `ctx.music_track` directly and
skips the track set/discovery logic. The named track plays as a
single-track loop (existing behavior).

Set rotation applies only when no explicit `--name` is given.

### 10. Edge Cases

**Empty library (first use):** `_discover_tracks()` returns `[]`.
The loop falls to the generation-first path, generates one track,
starts playing it, then spawns background fill for the remaining 2.
Identical to current behavior for the first track; fill is new.

**Mid-generation vibe change:** Cancel the in-flight generation task
(existing behavior). Build new track list for new vibe. If cached tracks
exist for the new vibe, start immediately. Otherwise generate-first.
No track from the cancelled generation is saved (consistent with
current behavior -- `_generate_music_track` writes only on success).

**Set size change:** The set size is set per `music_on` invocation.
Changing it mid-session requires `/music off` then `/music on
--set-size N`. If the new size is smaller than the existing track
count, the extra tracks remain on disk but only `music_set_size` tracks
are included in the rotation (keep the first N tracks in sorted order,
which is the N oldest by the `YYYYMMDD-HHMM` suffix).

**Disk full:** `_generate_music_track` raises `OSError`. The retry
logic handles it -- after 3 failures, music disables. Same as today.

**Corrupt track file:** ffplay exits with non-zero rc. The rotation
advances to the next track. The corrupt file stays on disk (no
auto-deletion).

**Concurrent sessions:** Only the music owner's session drives
generation and rotation. Non-owning sessions hear the music but
cannot change the track set (DES-031 unchanged).

## Changed Files

### `src/punt_vox/voxd.py`

- Add `music_tracks`, `music_track_index`, and `music_set_size` fields
  to `DaemonContext`.
- Remove `music_track` field (replaced by `music_tracks` list).
- Add `_discover_tracks(vibe, style) -> list[Path]` function.
- Rewrite `_music_loop` inner structure: track discovery, rotation
  index, background fill task, reshuffle on wrap.
- Update `_generate_music_track` to accept an explicit track name
  (decouple from `ctx.music_track_name` for background generation).
- Update `_handle_music_on`: parse `set_size`, set `ctx.music_set_size`.
- Update `_handle_music_vibe`: clear `ctx.music_tracks` so the loop
  rebuilds for the new vibe.
- Update `_handle_music_play`: set `music_replay=True`, bypass track
  set logic (no change from current behavior).
- Update `_handle_music_off`: clear `ctx.music_tracks`,
  reset `ctx.music_track_index`.
- Add `_DEFAULT_SET_SIZE = 10` constant.
- Update `_health_payload_full` to include track set info (track count,
  set size, current index).

### `src/punt_vox/__main__.py`

- Add `--set-size` option to `music on` command.
- Pass `set_size` through `VoxClientSync.music()`.

### `src/punt_vox/client.py`

- Add `set_size` parameter to `VoxClientSync.music()` and
  `VoxClient.music()`.

### `src/punt_vox/server.py`

- Add `set_size` parameter to the `music_on` MCP tool.
- Pass through to voxd via client.

### `tests/test_voxd_music.py` (new file or extend existing)

- Test `_discover_tracks` with various file layouts.
- Test track rotation (index advancement, reshuffle on wrap).
- Test background fill: mock `ElevenLabsMusicProvider.generate_track`,
  verify generation stops at `music_set_size`.
- Test vibe change mid-fill: verify old gen task cancelled, new
  track list built.
- Test empty library path.
- Test set_size clamping (0 -> 1, 100 -> 10).
- Test `music_replay` bypass (named track skips track set logic).

## Test Strategy

Unit tests for the new pure functions (`_discover_tracks`, index
advancement logic) are straightforward. The `_music_loop` integration
tests should use the existing pattern from the test suite: mock the
`ElevenLabsMusicProvider` and subprocess creation, drive the loop by
setting `DaemonContext` fields and signaling `music_changed`.

Key behavioral tests:

1. Start with 2 cached tracks, set_size=10. Verify: plays immediately,
   generates 1 more, then stops generating.
2. Start with 0 cached tracks. Verify: generates first track, starts
   playing, generates 2 more in background.
3. Vibe change with 3 cached tracks for new vibe. Verify: switches
   immediately, no generation.
4. Vibe change with 0 cached tracks. Verify: old track continues
   until new one is ready (DES-033 gapless).
5. Set fills to `music_set_size`. Verify: no further generation tasks
   created.
6. Named replay (`--name`). Verify: bypasses track set, loops single
   track.
