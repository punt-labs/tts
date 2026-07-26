---
description: "Set session mood for TTS voice"
argument-hint: "<mood> | auto | off"
allowed-tools: ["mcp__plugin_vox_mic__vibe", "mcp__plugin_vox_mic__status", "mcp__plugin_vox_mic__music"]
---

# /vibe command

Set a mood for all TTS speech this session. You interpret the mood and
translate it into ElevenLabs expressive tags.

## Usage

- `/vibe banging my head against the wall` — set manual vibe
- `/vibe auto` — return to automatic vibe detection (default)
- `/vibe off` — disable vibe entirely
- `/vibe` — show current vibe and mode

## Modes

**auto** (default): the agent keeps the vibe current from the
conversation. Every few user prompts a non-blocking reminder nudges the
agent to glance at how the session is going and, if the mood has clearly
shifted, set the vibe — using the same tag translation below. No
deterministic classification: the agent has the whole-session context
(the real success/failure signal) that a per-command hook never did.

**manual**: User-specified mood overrides auto-detection. The manual
mood takes priority when choosing tags at stop time.

**off**: No vibe tags applied. Voice is neutral.

## Your role: voice director

When the user sets a vibe, you translate it into 1-3 ElevenLabs expressive
tags that capture the mood. This is a creative interpretation — use your
judgment.

ElevenLabs eleven_v3 expressive tags are bracketed text that color delivery
for ~4-5 words. The model understands:

- Emotions: `[frustrated]`, `[excited]`, `[melancholy]`, `[smug]`, `[weary]`
- Actions: `[sighs]`, `[laughs]`, `[whispers]`, `[yawns]`
- Directions: `[dramatic tone]`, `[slow]`, `[rushed]`
- Scenes: `[announcing a winner]`, `[telling a secret]`

Examples of your translation:

| Mood / Signals | Tags you'd write |
|----------------|-----------------|
| `banging my head against the wall` | `[frustrated] [sighs]` |
| `just shipped a release` | `[excited]` |
| `3am and still debugging` | `[tired] [slow]` |
| `presenting to the board` | `[confident] [dramatic tone]` |

Keep it to 1-3 tags. Fewer is better — let the mood breathe.

When the auto reminder fires, read the mood the same way and pick tags:
`[happy]` when things are flowing, `[focused]` mid-problem, `[frustrated]
[sighs]` when stuck, `[relieved]` just after a fix, `[weary]` on a long
grind. Skip the update when the mood hasn't changed.

## Effect on music

If background music is *playing*, changing the vibe re-pools the music to the
new mood — but the `vibe` tool does not drive playback. Instead its reply
carries a `music_hint`: an instruction telling you to author the new pool and
call the `music` tool yourself. The mood *colors* the genre; the genre never
changes.

When music is *off*, the `vibe` reply has no `music_hint` and you do nothing
music-wise — a vibe change then only sets the speaking mood.

## Implementation

Use the `vibe` MCP tool for all writes. Use the `status` MCP tool for
status queries.

- **`/vibe <mood>`**: Interpret the mood, choose tags, then call:
  `vibe(mood="<mood text>", tags="<your tags>", mode="manual")`
- **`/vibe auto`**: `vibe(tags="", mode="auto")`
- **`/vibe off`**: `vibe(tags="", mode="off")`
- **`/vibe` (no argument)**: Call the `status` MCP tool and report current
  `vibe_mode`, `vibe`, and `vibe_tags`

**After every `vibe()` write, inspect the reply for a `music_hint`.** When it is
present the reply also carries `music.style` (the genre now playing). Do the
following — on the manual `/vibe` path *and* whenever the auto reminder makes
you set the vibe:

1. Read `music.style` from the reply.
2. Author exactly 12 genre-accurate `variations` plus a `base_prompt` for
   `(new mood × that style)` — the mood shades the genre, it never replaces it
   (relaxing flamenco = slow *soleá*/*guajira*; intense flamenco = fast
   *bulerías*). Follow the authoring rules in `/music`.
3. Call `music(subcommand="on", style="<that style>", base_prompt="…",
   variations=[… 12 …])`. The daemon rotates the pool for free if it already
   exists, or generates it if it is new.

No `music_hint` (music off) → do nothing music-wise. No text output after
changes — the panel confirms with the vibe shift.

## Proving the chain (observability)

The subsystem appends stable `[vibe-trace]` events to a durable, greppable log
file at `~/.punt-labs/vox/logs/vibe-trace.log`. Both emitters — the `mic` server
(vibe set / music) and the UserPromptSubmit hook (nudge fired) — are separate
processes that append to this one file with atomic `O_APPEND` writes, so lines
never interleave. Grep it to *prove* each link fired, or to catch a silent gap:

```bash
grep '\[vibe-trace\]' ~/.punt-labs/vox/logs/vibe-trace.log
```

Three event shapes, and what a matching pair proves:

- `[vibe-trace] nudge fired counter=5->0 mode=auto` — the auto-vibe reminder
  fired (from the UserPromptSubmit hook).
- `[vibe-trace] vibe set mood=relaxing mode=auto music_playing=true hint_emitted=true style=flamenco`
  — the vibe was set. `hint_emitted=true` means a `music_hint` was returned.
  `music_playing` is a tri-state: `true`/`false` from the daemon's playback
  status, or `unknown` when the status read failed (daemon down/protocol error)
  — an outage never reads as a false `false`.
- `[vibe-trace] music on style=flamenco vibe=relaxing prompts=authored` — the
  `music` tool re-pooled.

- **Auto-vibe works** when a `nudge fired` is followed by a `vibe set … mode=auto`.
  A `nudge fired` with no following `vibe set` = the agent ignored the nudge.
- **Vibe → music works** when a `vibe set … music_playing=true` is followed by a
  `music on`. A playing `vibe set` with no following `music on` = the agent
  dropped the hint.
