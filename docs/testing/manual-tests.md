# Manual Test Flight

Vox produces audio. Tests against logs and exit codes are not sufficient — the operator must hear the output and confirm it matches expectations. This document defines the canonical manual test flight: a short sequence that exercises the synthesis, cache, multi-segment, music, mix, recordings-store, and music-catalog paths through real audio hardware.

Referenced from `CLAUDE.md` inner-loop step 5 ("Exercise manually"). Run this whenever code in `src/punt_vox/voxd/`, `src/punt_vox/providers/`, `src/punt_vox/core.py`, `src/punt_vox/hooks.py`, or `src/punt_vox/server.py` changes in ways that affect runtime behavior.

## Hard rule: ask the operator after every audible step

Each step ends with a question to the operator about what they heard. **Ask immediately, not at the end.** Audio impressions decay within seconds — by the time the test flight is done the operator will not remember whether call 2 sounded identical to call 1, or whether the music ducked when voice played. If you batch the questions, the operator says "I don't remember" and the test result is unverifiable.

Use `AskUserQuestion` after each audible step. Wait for the answer before proceeding to the next step. If an answer reveals a defect, file a bead before moving on so the finding is captured while the audio is still fresh in everyone's head.

## Pre-flight

1. Working tree is clean except for the change under test.
2. `make check` passes.
3. `make install` builds and installs the wheel:

   ```bash
   make install
   ```

4. Confirm the installed env has today's code. Pick a symbol the change touched and verify it imports (the `uv`-tool path is home-relative, so this works for any operator):

   ```bash
   ~/.local/share/uv/tools/punt-vox/bin/python3 -c "from punt_vox import signal; print(signal.SignalLog.MAX_ENTRIES)"
   ```

5. Restart voxd on a temporary log so you're testing the just-built code. Use a graceful stop (SIGTERM, not SIGKILL) and tolerate "no process":

   ```bash
   pkill -f 'voxd --port' 2>/dev/null || true
   ~/.local/share/uv/tools/punt-vox/bin/voxd --port 8421 > /tmp/voxd.log 2>&1 &
   sleep 2
   pgrep -f 'voxd --port'  # must print a PID
   ```

6. `tail /tmp/voxd.log` must show `voxd listening on http://127.0.0.1:8421` and not an `ImportError`.

## Flight

Steps call the MCP tools directly (`mcp__plugin_vox_mic__unmute`, `mcp__plugin_vox_mic__music`) so the flight exercises the model-initiated path. Where a step uses the CLI instead (Step 3), it is called out explicitly.

### Step 1 — single synthesis, auto-picked provider

Call `mcp__plugin_vox_mic__unmute` with `text="Phase two signal extraction is live in voxd."`. Do not pass `voice` or `provider` — the daemon picks both from the algorithm (ElevenLabs > OpenAI > Polly > platform fallback, based on available keys) and the session config.

Confirm in `/tmp/voxd.log` that the synthesis line shows the expected provider (e.g. `provider=elevenlabs`).

**Ask the operator:** "Did the single voice play cleanly — no clipping, no stutter, voice matches the picked provider?"

### Step 2 — cache hit on identical text

Repeat step 1 with the exact same `text`. The cache key is `(text, voice, provider)`; identical inputs must hit the cache.

The cache hit is now an explicit, assertable signal — no log-grepping guesswork:

- **MCP result:** the `unmute` result JSON carries `"cached": true` for this call, versus `"cached": false` on the first (miss) call. The model driving the flight asserts this directly.
- **Log:** `/tmp/voxd.log` shows a distinct `cache HIT: ... file=<cache path>` INFO line on the second call, where the first call logged `cache MISS: ...`. No new synthesis runs on the hit.

The audio should be byte-identical to step 1.

**Ask the operator:** "Did this call sound identical to step 1 — same intonation, same pacing, no fresh synthesis variation?"

### Step 3 — long text, sentence splitting

Chunking only triggers when the text **exceeds the active provider's per-request character limit** — `eleven_v3` (default): 5,000; ElevenLabs turbo/multilingual: 10,000; `eleven_flash_v2_5`: 40,000; OpenAI: 4,096. Below that, `split_text()` returns the text as a single chunk and nothing is split. So this step needs a genuinely long input — a short sentence will not exercise the path.

Use the **CLI** (`vox say`) to generate a block that clearly exceeds the 5,000-char `eleven_v3` limit:

```bash
vox say "$(python3 -c 'print("This is the first sentence, long enough to carry weight. Here is a second sentence with several more words in it. And a third sentence to add length. " * 60)')"
```

`core.py:split_text()` splits on sentence boundaries; the daemon synthesizes each chunk in parallel and stitches them back into one stream. (To keep the listen short while still exercising split+merge, set `TTS_PROVIDER=openai` — its 4,096 limit chunks a smaller block — or run `vox rec new "<long text>"` then `vox rec get <id>` and inspect the single merged file.)

**Ask the operator:** "Did the whole passage play as one continuous stream with natural sentence pauses and no audible seam or gap at the chunk boundaries?"

### Step 4 — multi-voice, multi-segment

Call `mcp__plugin_vox_mic__unmute` with `segments=[{"text": "First segment from roger.", "voice": "roger"}, {"text": "Second segment from matilda.", "voice": "matilda"}]`.

This exercises the segment path: per-segment voice override, sequential playback, and a brief pause between segments (the tool's default `pause_ms`, 500 ms).

**Ask the operator:** "Did you hear two clearly distinct voices in order — male first, female second — with a brief pause between them?"

### Step 5 — music start

Call `mcp__plugin_vox_mic__music` with `mode="on"`. The first time per vibe, ElevenLabs Music generates a fresh ~2-minute track (10–15 seconds wall time). Subsequent calls reuse the cached track.

Confirm in `/tmp/voxd.log` that the music track is written under `~/Music/vox/tracks/`. Confirm the loop player is running: `pgrep -fl 'afplay|ffplay'` — the background player is `afplay --volume 0.3 .../<track>.mp3` on macOS, or `ffplay ... -volume 30 .../<track>.mp3` on Linux.

**Ask the operator:** "Is music playing at low volume? Does it sound like instrumental ambient material matching the session vibe?"

### Step 6 — voice over music (mix)

Wait 15 seconds so the music has clearly started, then call `mcp__plugin_vox_mic__unmute` with a short text. The voice should play at full volume on top of the music.

**Ask the operator:** "While the voice played, was the voice clearly intelligible over the background music, and did the music continue after the voice finished?"

> ⚠️ Current behavior is **static** ducking only — music plays at reduced volume (`--volume 0.3` / `-volume 30`) from music-on time and does not duck further during voice. Tracked in `vox-zuqr`. Note the operator's answer and file a bead if the behavior has regressed beyond the known baseline.

### Step 7 — music stop

Call `mcp__plugin_vox_mic__music` with `mode="off"`. Confirm the music player is gone: `pgrep -fl 'afplay|ffplay'` (macOS/Linux) should show no track player.

**Ask the operator:** "Did music stop cleanly when you sent the off command, no lingering bleed?"

### Step 8 — recording into the store, play it back

Capture a recording into the daemon store and play it on the daemon host. The
CLI `vox rec` group and the MCP `mic:rec_*` tools are the same engine ops:

```bash
id=$(vox rec new "This recording lives in the daemon store.")
echo "stored id: $id"     # bare store id, no path
vox rec list              # the new id appears, one per line
vox rec play "$id"        # plays on the daemon host (the machine with speakers)
```

**Ask the operator:** "Did the stored recording play back cleanly on the daemon host?"

### Step 9 — materialize a recording into the current directory

Fetch the bytes to the client and confirm the no-clobber guard:

```bash
cd "$(mktemp -d)"
vox rec get "$id"                    # writes ./$id in the current directory
ls -l "$id"                          # present
vox rec get "$id"; echo "exit=$?"    # refuses: target exists → non-zero exit, no overwrite
afplay "$id" 2>/dev/null || ffplay -autoexit -nodisp "$id"   # play the fetched file locally
```

The bytes stream from the daemon in bounded, sha256-verified chunks (any size).
Assert: the first `get` writes the file, the second refuses with a non-zero exit
and does not overwrite.

**Ask the operator:** "Did the fetched file play locally and sound identical to the stored recording?"

### Step 10 — music catalog: generate one track and play it

`vox music new` generates one track from a finished ElevenLabs prompt into a
fresh catalog album, without disturbing any running program (costs ElevenLabs
credits, ~10–15 s):

```bash
album=$(vox music new "warm analog synth pads, slow, D minor, no drums")
echo "album id: $album"
vox music list                # the album appears with a ready/total count
vox music play "$album"       # plays the album on the daemon host
```

**Ask the operator:** "Did the generated album play on the daemon host and match the prompt (warm analog pads)?"

### Step 11 — catalog guardrails (no audio to judge — assert the outcomes)

Confirm the daemon refuses hostile input and protects a playing album:

```bash
vox rec get "../escape.mp3"; echo "exit=$?"     # hostile ref → clean error, non-zero exit, nothing written
vox music remove "$album"; echo "exit=$?"       # album from step 10 is playing → refused ("is playing")
vox music off 2>/dev/null || true               # stop playback
vox music remove "$album"; echo "exit=$?"       # now stopped → removes cleanly (exit 0)
```

No audio — assert each: the traversal ref is refused and writes nothing, `remove`
of the playing album is refused with an "is playing" message, and after stopping
the remove succeeds.

## Post-flight

- Summarize: which steps passed, which raised findings.
- For every operator answer that revealed a defect, confirm a bead was filed.
- For every step that emitted unexpected error or warning log lines, capture them and decide whether to file.
- Stop the temporary test-flight daemon if you launched one: `pkill -f 'voxd --port' 2>/dev/null || true`. (This kills only a manually-launched instance; the installed LaunchAgent/systemd daemon is managed by `vox daemon restart`.)

## Known issues affecting this flight

- `vox-ekmx` — small clipping artifact at end of synthesized audio (missing trailing silence)
- `vox-zuqr` — music ducking is static, not dynamic during voice playback

When these are open, factor them in when interpreting operator answers. A "yes, clipping at the end" answer for step 1 is the known issue, not a new regression — unless the clipping is markedly worse or in a different place.
