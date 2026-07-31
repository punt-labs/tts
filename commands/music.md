---
description: "Control background music generation"
argument-hint: "on [--title ...] [style ...] | off | next | prev | pause | resume | play <name> | list | status"
allowed-tools: ["mcp__plugin_vox_mic__music", "mcp__plugin_vox_mic__status"]
---

# /music command

Control vibe-driven background music generation. `/music on` plays the first
track as soon as it is ready and then, with no further commands, generates the
rest of the `(vibe, style)` pool (up to 12 distinct tracks) in the background
and **auto-advances** to a different track as each one ends. Once the pool has
12, generation stops and playback **rotates** the pool (shuffled, never the
just-played track) at zero credits.

While music is playing, a vibe or style change re-pools the music: the current
song finishes, then playback switches to that mood's pool — rotating it in for
free if those tracks already exist, or generating them if the pool is new. When
music is **off**, a vibe change updates the session mood only; it does not start
or generate music. There is no confirmation and no credit prompt — the re-pool
is the intended effect of the mood change. (The `/vibe` skill drives this: a
vibe change while music plays returns a `music_hint`, and the agent authors the
new pool and calls this tool.)

**You author the prompts.** vox is a pipe to ElevenLabs; it never decides what a
genre sounds like. When you turn music on (and on any style/vibe change) YOU --
the agent, using your own genre knowledge -- write a `base_prompt` plus exactly
12 literal, genre-accurate `variations` (one per pool slot) and pass them to the
`music` tool. voxd generates track `i` from `base_prompt` + `variations[i]`, so
the 12 tracks are 12 distinct tracks *within the genre*. If you pass neither,
voxd falls back to a bare `"<style> music, <mood>. instrumental, loopable."`
prompt -- functional, but flavorless.

## Usage

- `/music on` -- start music with current vibe
- `/music on style techno` -- start music with a style modifier
- `/music on --title "Focus Beats"` -- start music and name the album "Focus Beats"
- `/music off` -- stop music
- `/music next` -- optional manual skip (playback auto-advances on its own): jump to the next track now — rotate the pool if it holds 12+ (zero credits), else generate a fresh one
- `/music prev` -- step back to the previous part of the now-playing album
- `/music pause` -- suspend the current album in place
- `/music resume` -- resume the suspended album in place
- `/music play <name>` -- replay a saved album by name
- `/music list` -- show saved albums with metadata
- `/music status` -- show current music state (same as `/music` with no argument)
- `/music` -- show current music state

## Album naming

Use `--title` on `on`/`new` to give the album a short, human name coherent with
the music (e.g. "Focus Beats"). The title becomes the album's `name` and rides
the ID3 `TALB`/`TIT2` frames. Absent a `--title`, voxd falls back to a
`{vibe}-{style}-{date}` slug (e.g. "happy-techno-20260412-1118"). To replay a
saved album by that name later, use `play <name>` -- it plays the existing album
without generation (zero credits).

## Style modifier

The style parameter persists in voxd. `/music on style techno` sets the
style; subsequent `/music on` (without style) reuses it. `/music on
style jazz` changes it.

## Implementation

> **Control actions produce NO agent text.** `on`, `off`, `next`, `prev`,
> `pause`, `resume`, and `play` are fire-and-forget: the `suppress-output.sh`
> hook and the audio panel are the whole response. After calling their tool,
> stop -- write no summary, no confirmation, no narration. Only the query
> actions (`list` and the no-arg/`status` state) return data for you to report.
> The hook enforces this; this line is the reminder behind it.

Parse `$ARGUMENTS`:

Every music action is ONE call to the `music` MCP tool, whose first argument is
`subcommand`
(`on`/`off`/`play`/`next`/`prev`/`pause`/`resume`/`list`/`status`/`new`/`get`/`remove`)
-- uniform with `vox music <subcommand>` on the CLI.

### `on` (with optional `--title ...` and `style ...`)

Call `music` with `subcommand="on"`. If the user provided style words after
`on style`, join them and pass as `style`. If `--title` is provided, pass as
`title` -- a short, human album name coherent with the music.

**Author the prompts.** Before calling the tool, write `base_prompt` plus
exactly 12 `variations` for the requested style (see "Authoring prompts" below)
and pass them. Re-author them whenever the style or vibe changes.

### `off`

Call `music` with `subcommand="off"`.

### `next`

Call `music` with `subcommand="next"`. Triggers regeneration while the current
track keeps playing (gapless).

### `prev`

Call `music` with `subcommand="prev"`. Steps back to the previous part of the
now-playing album.

### `pause`

Call `music` with `subcommand="pause"`. Suspends the current album in place.

### `resume`

Call `music` with `subcommand="resume"`. Resumes the suspended album in place.

### `play <name>`

Call `music` with `subcommand="play"` and the album name.

### `list`

Call `music` with `subcommand="list"` and display the album library.

### `status` (or no argument)

Call `music` with `subcommand="status"` and report current music state.

## Authoring prompts

You write the descriptions -- Python does not. Follow these rules:

- **Genre-forward and literal.** Lead with the genre and let it dominate. Name
  the real instruments, mode/scale, and forms of the style. "Klezmer, freylekhs
  dance, clarinet and violin, in D freygish" -- not "upbeat happy music".
- **Never name a specific artist, composer, band, or copyrighted work.**
  ElevenLabs Music **rejects** these under its Terms of Service -- the request
  fails with `bad_prompt` and no track is generated. Do NOT write "Chopin
  nocturne", "in the style of Aphex Twin", "Clair de lune", or any named
  person/title. Describe the music *itself* -- form, instruments, mode/scale,
  era, tempo, key, mood -- so the description evokes the same sound without the
  name: "romantic-era solo piano nocturne in E-flat major, lyrical right-hand
  melody over rolling left-hand arpeggios" -- not "Chopin nocturne".
- **Vary WITHIN the genre.** The 12 variations should be 12 distinct tracks of
  the *same* genre. Vary dance form, tempo (BPM), mode/key, lead-instrument
  emphasis, and mood shade. Do NOT drift toward genre-alien instruments or
  production -- a lo-fi Rhodes on a Klezmer pool is the bug.
- **`base_prompt`** is the stem shared by all 12: genre, core instrumentation,
  "instrumental, loopable". End it without trailing punctuation.
- **Each variation** is a short, self-contained clause voxd appends to the base.
- **Never** add generic "background music for deep work / smooth ambient texture
  that cycles / driving beat but not overwhelming / afternoon focus / steady
  working pace" boilerplate. That tail homogenizes every genre into smooth jazz.
- **Supply a `title`.** On `on`/`new`, also pass a short, human album name
  coherent with the music (e.g. "Klezmer Wedding"). It becomes the album's `name`
  and rides the ID3 `TALB`/`TIT2` frames. Absent a `title`, voxd falls back to a
  `{vibe}-{style}-{date}` slug.

### Worked example: `style Klezmer`, vibe "celebratory"

`base_prompt`:

> "Klezmer, traditional Ashkenazi Jewish folk, clarinet and violin lead with
> accordion and upright bass, acoustic, celebratory, instrumental, loopable"

`variations` (exactly 12):

1. "freylekh at 120 BPM in D freygish, clarinet lead, bright and dancing"
2. "bulgar at 132 BPM in G freygish, violin lead, driving hand percussion"
3. "hora at 96 BPM in A minor, accordion lead, lilting triple feel"
4. "doina rubato intro in C freygish, unaccompanied clarinet, mournful then rising"
5. "sher at 116 BPM in D minor, violin and clarinet trading the melody"
6. "khosidl at 88 BPM in G minor, stately accordion, dignified"
7. "terkish at 104 BPM in D freygish, clarinet ornaments over a habanera bass"
8. "freylekh at 140 BPM in E freygish, full ensemble, ecstatic wedding energy"
9. "honga at 100 BPM in A freygish, tsimbl (hammered dulcimer) accents"
10. "nign at 72 BPM in D minor, wordless singing feel on violin, contemplative"
11. "bulgar at 126 BPM in C freygish, clarinet krekhts (sobs), tight and punchy"
12. "kolomeyke at 150 BPM in G major, fiddle-forward, breakneck and joyful"

Every entry is Klezmer; they differ by form, tempo, mode, lead, and mood -- not
by drifting to another genre.

## Requirements

Music generation requires an ElevenLabs paid plan. Each track costs
approximately 2,000 credits (~3 minutes of audio). A typical session
generates 1-3 tracks (one per vibe change).
