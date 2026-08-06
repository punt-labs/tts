# Music Player — Phase 3: Transport Controls

Phase 3 adds a transport control bar to the `vox.music` scene — **prev · play/pause ·
next · stop** — that controls the *now-playing* album and introduces a real
**pause/resume** capability. It builds on the phase-1/2 player (the album-list scene,
the `PlayerView`, the publish/subscribe receive leg).

This is a **stateful-audio** change (a third player mode + suspend/resume + part
navigation), so it carries a Z model (`docs/vox-music-player-transport.tex`),
`fuzz`-clean and ProB-checked, *before* implementation.

## The player state machine

Three modes (the phase-1/2 player had two — `idle`, `playing`):

```text
        play(album)              pause
 idle ─────────────▶ playing ──────────▶ paused
   ▲                  │  ▲                  │
   │      stop        │  │     resume       │
   └──────────────────┴──┴──────────────────┘
                      (stop from playing OR paused → idle)

 playing│paused ──prev──▶ previous part  (cursor − 1, floored at 1)
 playing│paused ──next──▶ next part       (cursor + 1, capped at M)
 playing        ──(part ends)──▶ auto-advance (existing behavior)
```

- **play(album X)** — from `idle`, starts album X at part 1 (the album-list Play
  buttons; unchanged from phase 2).
- **pause** — from `playing`, suspends playback in place; the part cursor is held.
- **resume** — from `paused`, continues the suspended playback from where it stopped.
- **stop** — from `playing` or `paused`, halts and returns to `idle`.
- **prev / next** — from `playing` or `paused`, move the part cursor within the
  now-playing album (prev = previous part, next = next part).

The **pause/resume mechanism is an implementation decision** for the daemon
specialist (e.g. `SIGSTOP`/`SIGCONT` of the playback subprocess per DES-030, or a
playback-loop suspend) — this brief fixes the *behavior and invariants*, not the
mechanism.

## Invariants (to model)

- **T1 — single active source.** At most one album is active (playing or paused).
  (Extends phase-2 I1.)
- **T2 — now-playing iff active.** A now-playing cursor is present exactly when the
  mode is `playing` or `paused` (not `idle`). (Extends I2 to cover `paused`.)
- **T3 — paused is suspended.** `paused` ⟹ playback is not progressing (the cursor
  does not auto-advance while paused).
- **T4 — transition guards.** `pause` only from `playing`; `resume` only from
  `paused`; `prev`/`next` only from `playing` or `paused`; `play(album)` only from
  `idle`.
- **T5 — cursor bounds.** Whenever active, the part cursor ∈ 1..M. `prev` at part 1
  is a no-op; `next` at part M is a no-op (auto-advance past M is the existing
  end-of-album behavior, unchanged).
- **T6 — glyph reflects state.** The play/pause button shows `⏸` (U+23F8) iff
  `playing`, and `⏵` (U+23F5) iff `paused`. When `idle` the transport is inert
  (see the scene).
- **T7 — catalogued.** A played album is catalogued (phase-2 I3, unchanged).

## Wire (receive leg)

New pub/sub topics the in-scene buttons publish, decoded by the receive leg into
player events that call the daemon:

| Topic | Button | Daemon call |
|-------|--------|-------------|
| `music.prev` | `⏮` prev | `ProgramService.prev()` — previous part (**new**) |
| `music.pause` | `⏸` (while playing) | `ProgramService.pause()` (**new**) |
| `music.resume` | `⏵` (while paused) | `ProgramService.resume()` (**new**) |
| `music.next` | `⏭` next | `ProgramService.advance()` (**exists**) |
| `music.stop` | `⏹` stop | `off` (**exists**) |
| `music.play` | list Play | `replay_album` (**exists**, phase 2) |

The play/pause button is **one button** whose `publish` and glyph the projection
sets from the current mode: `playing` → `⏸` + `music.pause`; `paused` → `⏵` +
`music.resume`. No daemon-side toggle ambiguity — the button always publishes the
one unambiguous transition for the state it was rendered in.

The daemon gains `pause()`, `resume()`, and `prev()` alongside the existing
`advance()`, `off`, and `replay_album`.

## Scene (projection)

A transport **row** (a `group`, `layout=columns`) rendered in `vox.music` near the
now-playing status: four `ButtonElement`s carrying the verified media glyphs as
labels (operator-verified on the lux ImGui/macOS font stack) plus a tooltip and the
`publish` attribute:

- prev `⏮` U+23EE → `music.prev`, tooltip "Previous"
- play/pause `⏸`U+23F8 / `⏵`U+23F5 → `music.pause` / `music.resume`, tooltip
  "Pause"/"Play"
- next `⏭` U+23ED → `music.next`, tooltip "Next"
- stop `⏹` U+23F9 → `music.stop`, tooltip "Stop"

(Not `▶` U+25B6 — it renders undersized from Arial Unicode; U+23F5 is the matching
play.)

**Idle state:** when nothing is playing the transport is inert — the four buttons
render `disabled` (greyed). Playback starts from the album list's per-album Play
buttons (phase 2). At part 1, `prev` renders `disabled`; at part M, `next` renders
`disabled`.

The projection stays a pure, I/O-free function of `(albums, view, notice)` extended
with the transport state — `AlbumListScene` gains the transport row; the button
`publish`/glyph/`disabled` follow from the `PlayerView` mode + cursor. The
`PlaybackNotice` status slot (failed-click surfacing) is unchanged.

## Decomposition (specialist owns the write-set)

The design mission's output is the write-set. Expected touch points, for the
specialist to confirm or restructure: the daemon `ProgramService` (the `pause` /
`resume` / `prev` capabilities + the suspend mechanism); the receive leg
(`lux_subscription`, `player_events` — the new events + dispatch); the projection
(`scene`, the transport row); the `PlayerView` (a `paused` mode + the cursor already
present); and the `mic:music` / `vox music` surface (a `prev` / `pause` / `resume`
caller so each capability has a non-UI caller too, mirroring `next`).
