# The voxd Music Player

A headless app inside `voxd` that drives a lux scene: browse vox's saved album
catalog, play or stop a saved album, GNOME-Music-plain. vox is the speakers,
lux is the display, and the two are co-located on the user's machine. The player
adds no new audio machinery — it is a **projection of voxd's one active source
onto a lux scene, plus a command translator back**. Richer transport
(pause/next/prev/queue) layers on later with no architectural change.

This document is the design; the write-set it produces is the input to a later
implementation mission. The cross-repo architecture is **settled with the lux
agent** and is restated in §2 for grounding, not reopened.

## 1. What already exists, and what is actually new

The temptation is to read "play / stop / now-playing state model" as new
playback machinery. It is not. `voxd` already owns exactly-one-active-source and
every playback primitive the player needs:

| Player action | Existing voxd primitive |
|---------------|-------------------------|
| play a saved album | `ProgramService.replay_album(album_id)` — a single-album `SelectionPlayback` |
| stop | `ProgramService.off()` |
| now-playing | `ProgramService.status()` → `ProgramStatus.radio(..., NowPlaying(index, of))` |
| browse the catalog | `ProgramService.catalog_albums()` → `tuple[Album, ...]` |
| auto-advance within the album | `SelectionPlayback.rotate()` (the loop drives it on track-end) |
| refuse deleting a playing album | `active_backing_locators()` guards `MusicRemove` |

The single-active-source invariant already holds: `ActiveContext.current` is zero
or one source, and the `audio-programs.tex` model proves the Radio/off/rotate
transitions the player rides. So the player does **not** re-implement playback.

The genuinely new work is three things, none of them a playback state machine:

1. **A lux scene projection** — turn `(albums, now-playing)` into a scene and
   `PUT /scenes/vox.music`, re-pushing whenever voxd's state changes.
2. **An inbound command leg** — receive `music.play {album_id}` / `music.stop`
   from in-scene buttons over lux's pub-sub, and translate each to the primitive
   above.
3. **A voxd-internal change signal** — a small PubSub seam on `ProgramService`
   so the projection re-pushes on every state change (a play from any surface,
   an auto-advance, a catalog edit), keeping the scene truthful.

This scoping is the design's first substantive claim, called out for the leader
in §8.

## 2. Architecture (settled cross-repo — restated, not reopened)

vox is one engine (`voxd`) fronted by thin clients. The Music Player is a new
**app inside `voxd`**: the daemon is the audio host and now also the lux app
host. Per invariant 9 of `WORKFLOW.md`, work that touches audio and daemon-owned
state routes through the daemon, so the player and its lux connection live in
`voxd`, not in a client.

The lux boundary, as agreed with the lux agent (`claude:tty21`):

- **voxd holds lux's public `LuxRestClient`** — never `DisplayClient`.
  `DisplayClient` is lux's Hub-internal renderer client (guard-enforced, being
  deprecated for apps): a `DisplayClient` player renders but every click
  dispatches into a void, which the lux z-spec already flagged. `LuxRestClient`
  is the public, validated, typed surface; raw REST is forbidden (Jim's ruling —
  validation and typing).
- **Identity is `kind=app`, automatic via `LuxRestClient`.**
- **Send leg (push):** `PUT /scenes/vox.music` with the album-list scene,
  re-pushed on every state change.
- **Receive leg (pub-sub):** in-scene Play/Stop `ButtonElement`s whose Hub-side
  handlers publish `music.play {album_id}` / `music.stop`; voxd subscribes via
  the persistent `LuxRestClient` extension over WebSocket (luxd serves WS
  alongside REST on one port).

```text
        voxd (audio host + lux app host)
  ┌──────────────────────────────────────────────┐
  │  ProgramService  ── change signal ─▶ MusicPlayer
  │  (catalog, replay_album, off,        │   │  │
  │   status; one active source)         │   │  │
  │        ▲                             │   │  ▼
  │        │ replay_album / off          │   │  AlbumListScene (pure projection)
  │        └─────────────────────────────┘   │  │
  │                                          │  ▼
  │                        LuxScenePublisher ─┼─▶ PUT /scenes/vox.music ─▶ luxd
  │                        LuxSubscription  ◀─┼── music.play / music.stop  (Hub)
  └──────────────────────────────────────────┘
                   (public LuxRestClient — one WS+REST port)
```

The `MusicPlayer` is the only component that talks to both `ProgramService` and
lux. Everything the scene shows is derived from `ProgramService`; everything a
click does is a call into `ProgramService`. There is no player-owned playback
state to drift out of sync.

## 3. vox-side decomposition

A new package, `src/punt_vox/voxd/music_player/`, holds the app. One concern per
module (PY-IC-6, PY-OO-2); the facade is the only public name (PY-DP-10). The
`ProgramService` change signal is a small addition to the existing programs
package.

### 3.1 New package `voxd/music_player/`

| Module | Class | Responsibility |
|--------|-------|----------------|
| `__init__.py` | — | `__all__ = ["MusicPlayer"]` — the facade is the package's public API |
| `player.py` | `MusicPlayer` | The app/facade. Holds the `ProgramService` seam and the lux publisher; registers as a change listener; projects state → scene → push; (phase 2) dispatches inbound events → service calls |
| `player_view.py` | `PlayerView` | Frozen value: `mode ∈ {idle, playing}`, the playing `album_id`, and the `NowPlaying` cursor. Built from `ProgramStatus`. The object the Z model pins |
| `scene.py` | `AlbumListScene` | Pure projection `(albums, view) → Scene`. No I/O. Builds the lux element tree (per-album row + Play button, a Stop control, a now-playing marquee) from the public `LuxRestClient` element builders |
| `lux_scene_publisher.py` | `LuxScenePublisher` | Thin transport over `LuxRestClient`: `push(scene)` → `PUT /scenes/vox.music`. No business logic |

Phase 2 adds the receive leg to the same package:

| Module | Class | Responsibility |
|--------|-------|----------------|
| `player_events.py` | `PlayAlbum`, `StopMusic` | The inbound event objects — a discriminated union, each with `apply(service)` (polymorphic dispatch — oo.md *Polymorphism Over Conditionals*, PY-OO-6), so the coordinator dispatches by message, not an `if`-ladder |
| `lux_subscription.py` | `LuxSubscription` | The persistent WS subscribe loop over the `LuxRestClient` extension: decode `music.play` / `music.stop` into `PlayerEvent`s, and own the ~30s menu-callback lease renewed by contact. **Gated on lux's not-yet-pinned subscribe API** (§7) |

### 3.2 Change signal on `ProgramService` (voxd-internal, not lux-gated)

The scene must re-push whenever voxd's state changes — a play or stop from any
surface (CLI, MCP, or the lux button), an auto-advance on track-end, or a
catalog edit (`music new` / `music remove`). `ProgramService` today exposes state
only by read-per-call `status()`; it fires no notification.

Add a publish-subscribe seam (PY-DP-8), the minimum that keeps the projection
truthful:

- `ChangeListener` — a single-method Protocol (PY-DP-11, PY-TS-6):
  `notify_changed() -> None`.
- `ProgramService.on_change(listener: ChangeListener) -> None` — register.
- `ProgramService._notify_change() -> None` — fired after each applied command
  (in the `ControlChannel` single-writer, so notification is serialized with the
  state it reports) and after each auto-advance in the playback loop, and after a
  catalog mutation (`new` / `remove`).

`MusicPlayer` registers one listener; `notify_changed()` reads `status()` +
`catalog_albums()` and builds the `PlayerView` and `AlbumListScene`.

**The lux `PUT` must never run synchronously in the single-writer.**
`_notify_change()` fires inside the `ControlChannel` single-writer, so a
synchronous `PUT /scenes/vox.music` would let a slow or unreachable `luxd`
stall the daemon's playback control loop — a dead display would freeze audio.
`notify_changed()` therefore only *builds* the scene and hands it to an async
publish task via a **latest-wins** channel (a one-slot mailbox that coalesces to
the newest scene): the single-writer returns immediately, and `LuxScenePublisher`
drains the mailbox on its own task, where a lux timeout or `HubUnavailable` is
logged and dropped, never propagated back into audio control. Playback is never
hostage to the display.

This lives in phase 1 — it is a daemon-internal concern, independent of lux's
gated PRs, and a read-only scene is worthless if it lies.

### 3.3 Wiring (composition root)

The daemon composition root (`voxd/daemon.py`, via `voxd/programs/wiring.py`)
constructs `MusicPlayer(service, LuxScenePublisher(LuxRestClient(...)))`, calls
`service.on_change(player)`, and in `_lifespan`:

- **phase 1:** push the initial scene once the lux connection is up;
- **phase 2:** run `LuxSubscription` as a background task alongside the existing
  playback/control tasks, cancelled on shutdown like its siblings.

The `LuxRestClient` is injected at the composition root (as the ElevenLabs
producer already is), so tests drive the player with a fake lux client and a fake
`ProgramService` seam.

## 4. The playback / player state model

The player owns no playback state; it owns a **view** derived from the one active
source, plus the invariants the contract asks to pin. The view is a two-mode
machine over the catalog:

```text
PlayerView
  mode        : {idle, playing}
  album       : optional ALBUM        -- the album playing (≤ 1)
  nowPlaying  : optional (index, of)  -- the track cursor within that album

  invariants
    I1  at most one album playing        -- #album ≤ 1
    I2  now-playing present iff playing  -- mode = playing ⟺ #album = 1 ⟺ #nowPlaying = 1
    I3  a played album is catalogued     -- album ⊆ catalogued ids
```

The transitions are the projection of the already-proven Radio machine
(`audio-programs.tex`) onto the view:

| Player transition | Underlying primitive | View result |
|-------------------|----------------------|-------------|
| `PlayerPlay(album)` from idle | `replay_album` → `StartRadio` | `playing`, `album = {id}`, cursor at track 1 of M |
| `PlayerPlay(album')` while playing | `replay_album` → `SwitchSelection` | `playing`, `album = {id'}` — the old album is displaced, never two playing (I1) |
| `PlayerTrackEnd` | loop drives `SelectionPlayback.rotate` → `RadioRotate` | `playing`, same album, cursor advances |
| `PlayerStop` | `off` → `RadioOff` | `idle`, `album = ∅`, `nowPlaying = ∅` (I2) |
| album removed while playing | refused by `active_backing_locators` guard | no view change — a playing album cannot be removed |

The three invariants are exactly the contract's "at most one album playing;
now-playing present iff playing; stop returns to idle." They are consequences of
the single-active-source model, not new runtime checks: I1 because the channel
holds one source; I2 because `RadioOff` empties the cursor and `StartRadio`
fills it; I3 because the `System` invariant already contains a Radio selection in
the catalogued albums.

**One shared audio slot.** Because there is one audio device and one active
source, playing a saved album and running the generative program (`mic:music
on`) are mutually exclusive: `PlayerPlay` displaces a running program (it is a
`SwitchSelection`/`SwitchProgram` retarget), and starting a program blanks the
player's now-playing. This is the only coherent design with one device and is
why the player is a view over the one source, not a second player. Called out
for the leader in §8.

## 5. Two-phase split

Each phase is independently shippable and rollback-coherent.

### Phase 1 — catalog + album-list scene (push only)

**Starts when** lux's public `LuxRestClient` export merges (PR-3; the lux agent
pings us). Everything here is stable in lux today.

Deliverables:

- The `music_player` package modules `player.py` (push side only),
  `player_view.py`, `scene.py`, `lux_scene_publisher.py`.
- The `ProgramService` change-signal PubSub (§3.2) — voxd-internal, not
  lux-gated.
- Wiring: construct the player, register the listener, push the initial scene.

Result: a live `vox.music` scene showing the saved-album catalog and the current
now-playing, re-pushed on every state change. The scene may render Play/Stop
`ButtonElement`s whose Hub-side handlers publish `music.play` / `music.stop`, but
voxd is not yet subscribed, so a click is inert until phase 2. (If the public
`LuxRestClient` in PR-3 does not yet expose the button-publishes-event element,
the controls render in phase 2 with the receive leg; the album list and
now-playing do not depend on it.)

### Phase 2 — interactive

**Gated on** lux's menu model and persistent-extension (subscribe) PRs.

Deliverables:

- `player_events.py` (the `PlayAlbum` / `StopMusic` command objects) and
  `lux_subscription.py` (the WS subscribe loop).
- `MusicPlayer` dispatch: decode an inbound event, call `event.apply(service)`
  (`replay_album` / `off`); the change signal then re-pushes the scene.
- The **"Music" menu callback** — a session-callback with a ~30s lease renewed by
  contact — that opens the scene from lux's menu.
- Wiring: run `LuxSubscription` as a daemon background task.

Result: clicking Play plays that album; clicking Stop stops; the scene reflects
the change because the same change signal re-pushes.

## 6. Stateful-audio z-spec gate

`WORKFLOW.md` requires a `fuzz`-clean Z model, before the matching
implementation, for a stateful subsystem with 3+ modes and invariants across
transitions. Assessment, part by part:

- **Phase-1 scene projection — no new model.** It is a deterministic function
  `(catalog, active source) → scene`, re-pushed on change. That is a projection
  (formatting-class), which the gate's carve-out excludes. It introduces no
  audio-state transition beyond what `audio-programs.tex` already proves.
- **Phase-2 playback transitions — already proven.** `PlayerPlay` / `PlayerStop`
  / `PlayerTrackEnd` are `StartRadio` / `RadioOff` / `RadioRotate` on a
  single-album selection, and the "cannot remove a playing album" property is
  the `MusicRemove` derivation — all `fuzz`-clean and ProB-checked in
  `audio-programs.tex`. Re-modeling them would duplicate a proven model.
- **The player-view invariants — modeled now.** The new, stable content worth
  pinning is `PlayerView` and its three invariants (I1–I3 of §4) across the
  player transitions. `docs/vox-music-player.tex` states them as a small
  self-contained machine and proves each transition preserves them. This is the
  required model for the stable playback state machine, and it is independent of
  the lux API (a transition's trigger — a click or a CLI call — is immaterial to
  the model).
- **The connection / subscribe / lease lifecycle — deferred.** The phase-2 WS
  connection state (`disconnected → connected → subscribed → lease-renewed`) and
  the menu-callback lease do have 3+ modes and invariants (at most one live
  subscription; a click received while disconnected is dropped; the lease
  expires unless renewed by contact). But its transitions depend on lux's
  **not-yet-pinned** subscribe/lease API, so it cannot be modeled precisely now.
  It is flagged pending the lux API pin and modeled in a follow-on amendment to
  `vox-music-player.tex` before the phase-2 subscribe loop is implemented.

So: `docs/vox-music-player.tex` ships now with the `PlayerView` machine
(`fuzz -t` clean); the connection/lease lifecycle is a named, pending addition.

## 7. Dependencies and gating

| Deliverable | Gated on |
|-------------|----------|
| Phase 1 (scene projection, push, change signal) | lux public `LuxRestClient` export (PR-3) |
| Phase-1 in-scene buttons | PR-3 exposing a button-publishes-event element (else defer to phase 2) |
| Phase 2 (subscribe loop, dispatch) | lux persistent-extension (subscribe) PR |
| Phase 2 "Music" menu entry | lux menu-model PR |
| Phase-2 connection/lease Z model | lux subscribe/lease API pinned |

## 8. Decisions for the leader

Three items for the leader to ratify or escalate before the implementation
mission dispatches.

1. **Scope: the player is a projection, not new playback machinery.** The
   playback primitives (`replay_album`, `off`, `status`, `catalog_albums`, the
   remove-guard) already exist; the new work is the lux projection, the receive
   leg, and a voxd-internal change signal. Recommend scoping the implementation
   mission to those three, not a rebuild of play/stop/now-playing. *(This
   corrects the contract's framing that the playback state model is "the real
   substance.")*

2. **One shared audio slot.** The player reuses the one active source, so playing
   a saved album and running the generative program (`mic:music on`) are mutually
   exclusive, and the lux Stop stops whichever is playing. Recommend accepting —
   it is the only coherent design with one audio device. *(Design decision, not a
   fork; confirm.)*

3. **Z-spec scoping.** Model the `PlayerView` invariants now
   (`vox-music-player.tex`, `fuzz`-clean); reuse `audio-programs.tex` for the
   proven Radio transitions; defer the connection/subscribe/lease lifecycle model
   until lux pins its subscribe API. Recommend ratifying this split. *(My call
   under the delegated assessment; confirm or override.)*

## 9. Write-set (the design's output)

Phase 1:

- New package `src/punt_vox/voxd/music_player/`: `__init__.py`, `player.py`,
  `player_view.py`, `scene.py`, `lux_scene_publisher.py`.
- `src/punt_vox/voxd/programs/service.py` (+ `control_channel.py` / the loop) —
  the `ChangeListener` PubSub seam and `_notify_change()` firings.
- `src/punt_vox/voxd/daemon.py` / `voxd/programs/wiring.py` — construct the
  player, register the listener, push the initial scene.
- `pyproject.toml` — the public lux client dependency.
- Tests mirroring each new module; `ProgramService` change-signal tests; a
  scene-projection test asserting the element tree for a known catalog + view.

Phase 2:

- `src/punt_vox/voxd/music_player/player_events.py`,
  `src/punt_vox/voxd/music_player/lux_subscription.py`.
- `MusicPlayer` dispatch and the menu-callback registration.
- `daemon.py` — run the subscribe loop as a background task.
- Tests: event decode/dispatch, the lease renewal, the subscribe-loop lifecycle.

Docs:

- `docs/vox-music-player.tex` — the `PlayerView` Z model (phase 1); the
  connection/lease amendment (before phase-2 subscribe implementation).

## 10. Rejected alternatives

- **A second, independent player with its own playback state.** Rejected: one
  audio device means one active source; a second player would contend for the
  device and could play two things at once. The player is a view over the one
  source.
- **`DisplayClient` for the player.** Rejected (and settled): it renders but its
  clicks dispatch into a void; the public `LuxRestClient` is the only surface
  with a working receive leg.
- **Raw REST to luxd.** Rejected per Jim's ruling — the public `LuxRestClient`
  carries validation and typing; raw REST bypasses both.
- **Re-model the playback transitions in `vox-music-player.tex`.** Rejected: the
  Radio/off/rotate/remove machine is already `fuzz`-clean and ProB-checked in
  `audio-programs.tex`; the new model pins only the player-view invariants.
- **Poll `status()` on a timer to refresh the scene.** Rejected: a change-signal
  PubSub re-pushes exactly when state changes, with no polling latency or wasted
  pushes.
