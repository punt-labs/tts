# Music Player — Lux UI

**Status:** locked 2026-08-01 (operator-approved layout). This is the spec edt
builds the `vox.music` scene to. It supersedes the current flat
`[slug | Play]` row list.

## Layout

```text
┌─ Music ────────────────────────────────────────────────────┐
│                                                             │
│   Synthwave Dreams                                          │
│   Neon Nights                                   2 of 10     │
│                                                             │
│   0:47  ▮▮▮▮▮▮▮▮▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯  3:20                  │
│                                                             │
│             ⏮        ⏸        ⏭        ⏹                   │
│                                                             │
│  ───────────────────────────────────────────────────────   │
│                                                             │
│   ▼  Albums                                    18 albums    │
│                                                             │
│   Album                          Genre        Tracks        │
│   ─────────────────────────────  ──────────   ──────        │
│   ▶ Synthwave Dreams             Synthwave      10          │
│     Midnight K-Pop               K-Pop          12          │
│     Ambient Drift                Ambient         4          │
│     Cool Modal Jazz              Jazz           12          │
│     New Orleans Jazz             Jazz            6          │
│     Delta Blues                  Blues          12          │
│     …                                                       │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

Three regions, top to bottom: **now playing**, **transport**, a **collapsible
album table**.

## Now playing

- **Album name** (prominent), then the **song/track title** on the line below,
  with the position `N of M` right-aligned on the song line. Nothing else — no
  artist/genre repetition (that lives in the table).
- A **progress bar** `elapsed ▮▮▯▯ total`. This is the one live element, fed by
  mpv's `time-pos` polled over the IPC socket ~once a second and pushed to the
  scene. Everything else is static per track.
- **Idle:** this area reads "Nothing playing" and the transport greys out.

## Transport

- Four buttons: `⏮` prev · `⏸`/`⏵` play-pause · `⏭` next · `⏹` stop.
- The play/pause glyph reflects state: `⏸` while playing (click pauses), `⏵`
  while paused (click resumes) — one button, one unambiguous transition.
- **No text labels** — the glyphs are self-evident; tooltips on hover.
- Publishes the existing receive-leg topics: `music.prev`,
  `music.pause`/`music.resume`, `music.next`, `music.stop`. The `off`→`stop`
  verb rename keeps `music.stop` wired to the daemon.

## Albums

- A **collapsible table** (`▼`/`▶` header) with an album count, columns
  **Album · Genre · Tracks**. No Artist column — generated music has no
  meaningful artist.
- The now-playing album's row is marked `▶`.
- **Click a row to play** — publishes `music.play` with the album id. No per-row
  Play button, no "click to play" legend; the interaction is self-evident.

## Data — ID3 and friendly names (a generation fix, not just a scene fix)

The table and now-playing render **ID3**, so the scene is only as good as the
tags the generator writes — and today it writes none of these:

- **Album name** — a human title (e.g. "Synthwave Dreams"), not the slug
  `synthwave-20260726-0326`, and never blank (three albums currently render as
  `album c1d7d6`).
- **Song/track title** — a per-part title in each mp3's ID3 `title` tag, not the
  filename `002.mp3`.
- **Genre** — the style; already meaningful.

The scene renders whatever ID3 is present. The generation change is what turns
the mockup from a debug dump into a real player, so it is part of this work, not
a separate follow-up.

## Build

- **edt** owns the `vox.music` scene (the element tree); **dna** reviews the UX.
- The **generation-naming** ID3 change is a daemon/generation fix (rmh/gvr).
- Preserve the receive-leg publish wiring; drive the Lux player end-to-end after
  every change (publish each topic, confirm the daemon acts).
