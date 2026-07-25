# Vox (text-to-speech)

Vox speaks for you. It exposes the `mic` MCP server, a set of `/` slash
commands, and Claude Code hooks that chime or narrate as a session runs.
This doc is how an *agent* drives vox — not how to develop vox itself.

Never `Read`, `Write`, or `Bash` the config files
`.punt-labs/vox/vox.md` or `.punt-labs/vox/vox.local.md`. All state is
reachable through the `mic` tools; touching the files directly races the
daemon.

## Speaking

- `mic:unmute` — synthesize and play text. Pass `text` (or a `segments`
  list for multi-voice). Mood tags are resolved from config; do not pass
  `vibe_tags` yourself unless the user asked for a specific delivery.
- `mic:speak` — toggle spoken notifications: `mode="y"` (voice) or
  `mode="n"` (chimes only).
- `mic:notify` — set the notification level: `"y"` (on task completion +
  permission prompts), `"n"` (off), `"c"` (continuous — also announces
  real-time signals).
- `mic:who` — list voices for the current provider (featured + full roster).
- `mic:status` — current provider, voice, notify/vibe state, and the
  authoritative music Program (read fresh from the daemon).

## Vibe (voice direction)

`mic:vibe` sets the session mood. You are the voice director: translate a
mood into 1–3 ElevenLabs expressive tags (`[frustrated]`, `[excited]`,
`[weary]`, `[sighs]`). `mode="manual"` pins your tags; `mode="auto"`
(default) lets tags update from session signals at each task completion;
`mode="off"` is neutral.

## Recordings

The daemon owns a recordings store; you address a recording by its bare store
id, never a path.

- `mic:rec_new` — synthesize `text` into the store; returns the bare id.
- `mic:rec_list` — list the stored recordings.
- `mic:rec_play` — play a stored recording (by id) on the daemon host.
- `mic:rec_get` — return a recording's bytes (base64) by id.
- `mic:rec_remove` — delete a recording by id.

## Music

`mic:music` drives vibe-matched background music (`music_play`,
`music_list`, `music_next`, `music`). **You author the prompts** — vox is
a pipe to ElevenLabs, it does not decide what a genre sounds like. On
`mode="on"` (and on every style/vibe change) write a `base_prompt` plus
exactly 12 genre-accurate `variations`, one per pool slot.

- Vary *within* the genre (form, tempo, mode, lead instrument, mood) — never
  drift to genre-alien instruments.
- **Never name an artist, band, composer, or copyrighted work.** ElevenLabs
  rejects those (`bad_prompt`) — describe the music itself instead.
- Music needs an ElevenLabs paid plan (~2,000 credits per ~3-minute track).
  Playback auto-advances as tracks end; `music_next` is an optional manual skip.

Catalog verbs (address a saved album by the id `music_list` prints):

- `mic:music_new` — generate ONE track from a finished, verbatim prompt into a
  fresh single-track catalog album. It does not disturb the running program.
- `mic:music_get` — export a saved album by id into a destination directory
  you name (`dest`); returns the written path/locator.
- `mic:music_remove` — delete a saved album by id (refused while it is playing).

## Slash commands

- `/vox y|n|c` — notification level; `/vox model <name>` / `/vox provider <name>`
  switch TTS engine mid-session.
- `/unmute [voice]` — enable voice mode, optionally set the session voice;
  `/unmute` (no argument) browses the roster.
- `/mute` — chimes only (spoken notifications off).
- `/vibe <mood>|auto|off` — set session mood.
- `/music on|off|next|play <name>|list` — background music.
- `/recap` — speak a 2–3 point summary of your last response.

## Stop-hook continuation

When a Stop hook blocks with a `♪` phrase, write 1–2 sentences summarizing
what you just completed, then call `mic:unmute` with `ephemeral=true`. Mood
tags are already resolved in config — do not pass `vibe_tags`. Emit no other
output; the audio panel confirms.

Voice supplements text — it never replaces it. Show the summary in the
conversation as well as speaking it.
