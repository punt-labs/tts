# vox-h777 — live track counts and element-level widget refresh

Two defects in vox's Lux surfaces. Both live on the refresh path, both are one
PR, and both have the same shape: **the widget reports state it captured once
instead of state as it stands.** Defect 1 freezes a number; defect 2 freezes the
whole tree and re-installs it, taking the frame's stacking order with it.

This note fixes both and hands back the write set.

## 0. Scope correction

The mission context reads *"`src/punt_vox/panel/applet.py` builds the element
tree; `lux_scene_publisher.py` pushes it."* That is not the code. There are
**two** independent scenes with two independent push paths:

| Scene id | Tree built by | Pushed by | Runs in |
|---|---|---|---|
| `vox.music` | `voxd/music_player/scene.py:53` | `voxd/music_player/lux_scene_publisher.py:74` | `voxd` |
| `vox.panel` | `panel/panel_scene.py:67` | `panel/service.py:113` | `vox-panel` applet |

`panel/applet.py` builds no elements; it assembles the applet (125 lines, all
wiring). Defect 1 is entirely in the `vox.music` path. Defect 2 is present,
identically, in **both** — `panel/service.py:113` calls `client.scene.show()` on
every control change and every settings refresh, so every radio click re-raises
the panel frame exactly as every track change re-raises the music frame.

**Decision: both scenes are in scope.** One mechanism, one shared collaborator,
one PR. Excluding the panel would ship the fix for the harder case and leave the
easier one broken — and the panel's element roster is already invariant, so it
is the cheapest possible instance of the same change.

Everything below cites `punt_lux` **0.28.0**, the version vox builds against
(`pyproject.toml:44`, resolved to `.venv/.../punt_lux/`). The working checkout at
`../lux` is a newer tree that no longer carries `LuxClient`; do not read the fix
against it.

---

## 1. Defect 1 — the Tracks column is frozen at mint time

### 1.1 Evidence

`voxd/music_player/album_display.py:45-47`:

```python
@property
def track_count(self) -> int:
    """Return the number of manifest parts -- the Tracks column cell."""
    return len(self.album.manifest.parts)
```

`Album.manifest` is the creation-time snapshot. `Album`'s own docstring
(`voxd/programs/catalog.py:33-42`) states the rule this property breaks:

> its `manifest` snapshot carries only the *durable* metadata … while its Parts
> are a *disk read*. The background fill grows the on-disk manifest after the
> catalog registers the album, so a frozen parts snapshot would go stale the
> instant the fill writes. `read()` and `ready_parts()` therefore dereference the
> store live; **the snapshot's own `parts` are never consulted for playback
> state.**

An album is minted with zero parts and filled in the background, so the cell
reads `0` for the album's entire life. Meanwhile the position line two elements
above it reads `3 of 12` from live `ProgramStatus`. The widget shows two
contradictory numbers about the same album, 40 pixels apart.

### 1.2 Full audit — is any other cell frozen the same way?

Every `.manifest` read reachable from the scene, and its verdict:

| Site | Reads | Live? | Verdict |
|---|---|---|---|
| `album_display.py:47` | `manifest.parts` | **no** | **the defect** |
| `album_display.py:42` | `manifest.tags.style` | n/a | correct — tags are durable, never mutated |
| `album_display.py:37` | `album.id` | n/a | correct — durable |
| `album_names.py:58` | `manifest.tags.display_title()` | n/a | correct — durable |
| `playback_notice.py:49` | `manifest.tags.name` | n/a | correct — durable (but see §6) |
| `album_table.py:84` | `len(self.albums)` | yes | correct — in-memory catalog size |
| `now_playing_block.py:119` | `cursor.index`/`cursor.of` | yes | correct — live `ProgramStatus` |
| `player_view.py:108` | `album.locator` | n/a | correct — durable |

`track_count` is the sole offender. `parts` appears nowhere else in the widget
path (`grep -n '\.manifest\b' src/` — 8 widget-path hits, listed above).

### 1.3 Why the one-line fix is wrong

The naive fix is `len(self.album.ready_parts())`. Do not do this. Three reasons,
each independently disqualifying:

1. **It puts disk I/O inside a documented pure projection.**
   `scene.py:1-12` declares `AlbumListScene` *"a deterministic, I/O-free function
   of its inputs (the gate's projection carve-out), reading only in-memory
   manifest data."* `Album.ready_parts()` → `store.open(locator)` →
   `filesystem_store.py:130-146` → `PathStatus.of()` stat + `O_NOFOLLOW` read +
   `AlbumManifest.from_json`. That is a stat, a read, and a JSON parse **per
   album, per projection**, and `AlbumDisplay` is constructed per row
   (`album_table.py:129`). An 18-album catalog pays 18 of them.

2. **That I/O lands on the control-channel single-writer thread.**
   `player.py:46-53` — `notify_changed` is contracted as *"fast, synchronous,
   non-blocking work … so the control-channel single-writer that fires the
   notification is never held up."* A projection that reads 18 files is not that.

3. **It introduces a raise path into the render.** `store.open` raises
   `LookupError` for a deleted album (`filesystem_store.py:134-144`). A row
   render that can raise turns a vanished album into a dead widget.

### 1.4 The fix — read once, at the seam; the projection stays pure

Move the live read *out* of the projection and *up* to the object that already
reads live daemon state. `MusicPlayer._submit` (`player.py:90-93`) already calls
`self._service.status()` and receives `catalog_albums()`; it is the correct — and
only — place where the widget touches live state today.

**`AlbumDisplay` holds the count; it does not fetch it.**

```python
@final
@dataclass(frozen=True, slots=True)
class AlbumDisplay:
    """One album's display cells: its genre, its live track count, and its id."""

    album: Album
    tracks: int          # ready Parts as of this projection — never the snapshot

    @classmethod
    def read(cls, album: Album) -> Self:
        """Pair ``album`` with its ready-Part count, read live from the store."""
        return cls(album, len(album.ready_parts()))
```

`track_count` becomes the `tracks` field. `genre` and `id` are unchanged — they
read durable metadata and are correct as they stand.

**`AlbumRoster` is the one place the read happens** (new,
`voxd/music_player/album_roster.py`):

```python
@final
class AlbumRoster:
    """The catalog as display rows: each album paired with its live track count.

    The single seam where the scene's per-album data leaves the store. Reading
    here, once per projection, is what keeps AlbumListScene the I/O-free pure
    projection it claims to be — and keeps a deleted album's LookupError out of
    the render path.
    """

    @classmethod
    def read(cls, albums: tuple[Album, ...]) -> Self: ...

    @property
    def albums(self) -> tuple[Album, ...]: ...      # the readable subset
    @property
    def displays(self) -> tuple[AlbumDisplay, ...]: ...
```

`AlbumRoster.read` catches **`LookupError` only** — the store's documented
"album was deleted" contract — logs at `debug`, and drops that album from *both*
`albums` and `displays`, so the table, `AlbumNames`, and `PlayerView` all see one
coherent snapshot. Every other exception propagates: a transient `OSError`
(EMFILE, a permission blip) silently vanishing an album from the widget is
exactly the failure `silent-failure-hunter` exists to catch.

Call-site change, `player.py:90-93`:

```python
def _submit(self, albums: tuple[Album, ...], notice: PlaybackNotice) -> None:
    roster = AlbumRoster.read(albums)
    view = PlayerView.from_status(self._service.status(), roster.albums)
    self._publisher.submit(AlbumListScene(roster, view, notice).render_request())
```

`AlbumListScene` and `AlbumTable` take the roster instead of the bare album
tuple. `AlbumTable._row` stops constructing `AlbumDisplay(album)` and reads the
one it was handed.

### 1.5 The coherence property this buys

`NowPlaying.of` is *"total Parts currently in the pool"*
(`types_programs/status_views.py:34`), and for a single-album replay it is
derived from `active.store.ready_parts()` (`voxd/programs/service.py:287`) —
the same live set `AlbumDisplay.read` counts. After the fix, for the playing
album:

```text
Tracks cell  ==  the M in "N of M"
```

That is a named, testable invariant, and today it is visibly false (`0` vs `12`).
It is the regression test for this defect.

---

## 2. Defect 2 — every refresh re-installs the scene

### 2.1 The mechanism, and the reframe

`lux_scene_publisher.py:74` and `panel/service.py:113` both call
`client.scene.show()`. `show` → `SceneOperations.render` → `install`
(`operations/scenes.py:87-118`) → Hub-side `upsert_scene_in_frame`, which raises
and unminimizes the frame whenever a scene id looks new to it. `update`
(`operations/scenes.py:120-170`) reaches `HubSceneWriter.apply` and never touches
frame, focus, or tab state at all. vox calls `update` nowhere.

The reframe that decides the design: **`show`'s frame-raise is not a bug — it is
the feature the menu entry depends on.** `LuxSubscription.on_callback:227-235`
exists so that clicking **Music** brings the window up. Replacing every `show`
with `update` would break that. The defect is that **one call is serving two
different intents**: *"put this window in front of the user"* and *"the numbers
in the window you are already looking at have changed."*

The fix separates them.

### 2.2 Intent table — which trigger gets which verb

`vox.music` (7 triggers):

| Trigger | Site | Verb | Why |
|---|---|---|---|
| Hub handshake — first paint, every reconnect | `lux_subscription.py:237` | `show` | nothing is installed |
| **Music** menu click | `lux_subscription.py:227` | `show` | the user asked for the window; raising it *is* the answer |
| Change signal — track change, part generated, catalog add/remove, pause/resume | `player.py:46` | `update` | the window is already where the user put it |
| Play refused | `player.py:55` | `update` | the user just clicked *in* this window |
| Stop refused | `player.py:65` | `update` | as above |
| Anchor unresolved | `player.py:69` | `update` | as above |
| Transport refused | `player.py:81` | `update` | as above |

`vox.panel` (3 triggers):

| Trigger | Site | Verb | Why |
|---|---|---|---|
| **Vox** menu click — the visible half | `panel/service.py:94` `acknowledge` | `show` | the user asked for the window |
| Confirm push, ~ms after the click | `panel/service.py:99` `service` | `update` | same window, already raised |
| Control change re-push | `panel/panel_guard.py:105` `repush` | `update` | the user is mid-interaction with the open widget |

Two `show` sites per scene. Everything else patches.

### 2.3 What `update` can and cannot do — the constraints that shape the fix

Read out of `punt_lux` 0.28.0, not assumed:

| # | Constraint | Evidence |
|---|---|---|
| C1 | A patch **sets fields on an existing element or removes it. It cannot add one.** `PatchBatch` has exactly `field_patches` and `removals`. | `domain/hub/patch_batch.py:33-36` |
| C2 | `children` / `tabs` are **refused** — the seam rebinds a value, it cannot install child elements. Answer is *"resend the whole tree via show."* | `domain/hub/deferral_errors.py:24-39` |
| C3 | A field with no `_set_<field>` method is rejected: *"cannot set unknown field"*. No `_set_handlers`, `_set_kind`, `_set_key_column`, `_set_selection_mode` exist. | `domain/hub/field_realization.py:82-88` |
| C4 | **Setters run in dict order** within one element's patch. | `domain/element_abc.py:202-226` |
| C5 | `table._set_selected_row_ids` intersects the ids **against the live rows at setter time** and does not re-check afterwards. `combo`/`radio` validate `selected` against `items` **after** the whole patch. Asymmetric. | `protocol/elements/table.py:219-234`; `protocol/elements/combo.py:10-13` |
| C6 | A rejected batch **mutates nothing** — store untouched — so falling back to `show` is always safe. | `domain/hub/scene_writer.py:75-104` |
| C7 | The connection id is derived from the **declared identity**, not the socket, so a freshly built `LuxClient` patches the scene an earlier client installed. This is what makes the panel's per-click `_rest_factory()` safe. | `connection_identity.py:39-45` |
| C8 | `client.scene.update(scene_id, UpdateRequest)` exists on the facade and accepts `UpdateRequest \| OpError`. | `client/scene.py:60-66` |

C1 is the one that forces work elsewhere: **if the element roster changes, no
patch can express it.** Two consequences follow.

### 2.4 Consequence A — the `vox.music` roster must become invariant

`NowPlayingBlock.elements()` (`now_playing_block.py:83-88`) emits a *different
set of ids* per mode:

```text
idle    →  [music.now]                              1 element
active  →  [music.now.album, music.now.position]    2 elements
```

So every idle↔active transition — the moment a user presses play, the most
consequential refresh there is — would fall back to `show` and raise the window.
That is the reported bug, unfixed.

**Fix: two slots, always present, content varies.**

| id | kind | active | idle |
|---|---|---|---|
| `music.now.album` | markdown | `### {escaped name}` | `### Nothing playing` |
| `music.now.position` | text | `{index} of {of}` | `""` |

`music.now` is deleted outright (no alias, no fallback — PL-PP-1).

This is better information design, not merely a mechanical accommodation. Today
the idle state answers "what is playing?" with a **body-weight line at a
different position**, so the reader's eye has to re-find the answer every time
playback stops or starts. After the change the answer lives in one slot, at one
position, at one type scale, always — only its value changes. The slot's
question is constant; that is what makes a display scannable.

The empty position line costs one blank text line (~19px). That trade is already
made, deliberately, twice in this codebase for exactly this reason:
`music.status` renders empty when the notice is silent (`scene.py:41-46`:
*"the scene shape is the same whether or not a click failed"*) and
`vox.panel.status` does the same (`panel_scene.py:5-9`). This is consistency
with a settled, operator-accepted decision, not a new cost.

`NowPlayingBlock` loses its mode branch: `elements()` returns two elements
unconditionally, and the idle/active choice collapses into the content of each.
That is a complexity paydown the ratchet will register.

`vox.panel`'s roster is **already** invariant — 9 elements, fixed ids, no
conditional shape (`panel_scene.py:70-82`). No change needed there.

### 2.5 Consequence B — field sets must be total, not just id sets

A differ that compares "the fields present in each render" cannot see a field
that **disappears**. `AlbumTable._selection` (`album_table.py:111-120`) returns
`{}` when idle and `{"selected_row_ids": [name]}` when playing. Going
active→idle, the key vanishes, the differ emits nothing, and the stale row stays
highlighted — a wrong now-playing indicator that survives until the next `show`.

**Fix: `_selection` always returns the key**, `{"selected_row_ids": []}` when
idle. An empty list is legal on install and on patch (`TableWire.str_list` of
`[]` → empty frozenset).

Generalize it as a rule the differ enforces: **a field present in one render and
absent in the other is a roster change ⇒ `show`.** The rule is the safety net;
the total field set is what keeps the net from ever catching.

### 2.6 The diff

Compare vox's own two wire trees — the previously pushed `RenderRequest` and the
new one. Never inspect Hub state; a read-back would race the replicator.

```text
plan(previous, current):
    if previous is None:                          -> Install     # nothing on screen
    if current.title/layout/frame != previous's:  -> Install     # not patchable
    flatten both trees to {id: element}, descending through
        "children" and "tabs" rather than comparing them (C2)
    if the two id sets differ:                    -> Install     # C1: no insertion
    for each id, in tree order:
        changed = {field: current[field]
                   for field in current_fields | previous_fields
                   if field not in ("kind", "id", "children", "tabs")
                   and current.get(field) != previous.get(field)}
        emit ElementPatch(id, changed) if changed
    if no patches:                                -> NoPush      # nothing changed
    otherwise:                                    -> Patch
```

Three notes on the algorithm:

- **Flatten on id, descend through containers.** `music.transport` is a group
  whose four buttons change; the patch addresses `music.transport.prev` etc., and
  never the group (C2). Same for `vox.panel.voice.row`.
- **Field order is preserved from the current element's dict.** For the table
  that means `rows` precedes `selected_row_ids`, because
  `AlbumTable._table` emits `rows` at `album_table.py:103` and the selection last
  via `**self._selection(names)` at `:108`. C4 + C5 make this load-bearing: patch
  the selection before the rows and the new ids are intersected against the
  *old* row set and silently dropped. **This must be an explicit test, not an
  accident of dict literal order.**
- **No allowlist of patchable fields.** Constraint C3 tempts one; resist it — a
  hand-copied list of lux's setters is a second source of truth that drifts. Rely
  on C6 instead: an unpatchable field produces a rejection that mutates nothing,
  and the fallback re-installs. Self-correcting beats synchronized.

### 2.7 Exactly which ids and fields get patched

**`vox.music`** — 11 addressable elements after the roster fix:

| id | kind | patchable fields the differ can emit | changes when |
|---|---|---|---|
| `music.now.album` | markdown | `content` | album changes; idle↔active |
| `music.now.position` | text | `content` | **every track change**; pool grows |
| `music.status` | text | `content` | a click fails; next change clears it |
| `music.transport` | group | — *(descended into, never patched)* | — |
| `music.transport.prev` | button | `label`, `tooltip`, `disabled`, `publish` | idle↔active; cursor reaches part 1 |
| `music.transport.playpause` | button | `label`, `tooltip`, `disabled`, `publish` | playing↔paused↔idle (glyph **and** topic flip) |
| `music.transport.next` | button | `label`, `tooltip`, `disabled`, `publish` | idle↔active; cursor reaches part M |
| `music.transport.stop` | button | `label`, `tooltip`, `disabled`, `publish` | idle↔active |
| `music.sep` | separator | — | never |
| `music.albums.label` | text | `content` | album added/removed (`Albums · N albums`) |
| `music.albums` | table | **`rows`, then `selected_row_ids`** | **defect 1's live count**; album added/removed; now-playing row moves |

Every one of these has a setter in 0.28.0 — verified: `markdown._set_content`,
`text._set_content`, `button._set_label/_set_tooltip/_set_disabled/_set_publish`,
`table._set_rows/_set_selected_row_ids`. The play/pause button's `publish` topic
flips between `music.pause` and `music.resume` (`transport_row.py:115`), so
`_set_publish` being present (`protocol/elements/button.py:181-183`) is
load-bearing, not incidental.

**`vox.panel`** — 11 addressable elements, all ids already fixed:

| id | kind | patchable fields | changes when |
|---|---|---|---|
| `vox.panel.status` | text | `content` | a voxd refusal or write failure |
| `vox.panel.notify` | radio | `selected` | Notifications changed |
| `vox.panel.sep1` | separator | — | never |
| `vox.panel.mic_mode` | radio | `selected` | Mic Mode changed |
| `vox.panel.sep2` | separator | — | never |
| `vox.panel.voice_engine` | text | — | never (constant label) |
| `vox.panel.provider` | combo | `selected` | provider changed |
| `vox.panel.model` | combo | `items`, `selected` | **provider changed → the model list changes with it** |
| `vox.panel.voice.row` | group | — *(descended into)* | — |
| `vox.panel.voice` | combo | `items`, `selected` | roster refresh; voice changed |
| `vox.panel.voice.preview` | button | — | never |

The model combo is the panel's version of the ordering hazard, with a helpful
difference: `combo` validates `selected` against `items` **after** the whole
patch (C5), so `items`+`selected` in one batch is order-insensitive and an
out-of-range pair is rejected whole — falling back to `show`. Only the **table**
needs the explicit ordering guarantee. State that asymmetry in the differ's
docstring; it is the kind of thing that gets "simplified" away.

### 2.8 The state machine

One value, `_previous: RenderRequest | None`, is the whole state. `None` means
"not installed on this connection"; no second boolean.

| State | Event | Action | Next |
|---|---|---|---|
| not installed | any push | `show` | installed |
| installed | refresh, tree identical | *nothing* — no HTTP, no repaint | installed |
| installed | refresh, fields differ | `update` | installed |
| installed | refresh, roster/shell differs | `show` | installed |
| installed | explicit install (menu click, handshake) | `show` | installed |
| installed | `update` returns `OpError` | `show` (lux's documented recovery, C6) | installed |
| any | `HubUnavailableError` | drop client, forget | not installed |

The **no-change → no push** row is a real win, not a micro-optimization. The
change signal fires on every catalog add/remove and every generated part; many
of those alter nothing the widget shows. Today each one is a full tree
re-install. After the fix each one is zero bytes on the wire.

`OpError` → `show` also covers the luxd-restart case without special handling:
the publisher's REST client never errored, so it still believes the scene is
installed; the first `update` after the restart hits `UnknownSceneError`, is
rejected whole (store untouched, C6), and the fallback installs. One wasted
round-trip per restart, no stale state.

---

## 3. OO decisions

The mission asks whether extraction is warranted. It is — but only three
classes, and each earns its place.

**Extract (new, `src/punt_vox/lux_common/` — the package is already
*"Shared building blocks used by Lux surfaces (panel, music player)"*, holding
`HubOutageLog` and `LuxNotice` on exactly that basis):**

| Module | Class | Responsibility |
|---|---|---|
| `scene_patch.py` | `ElementPatch`, `ScenePatchSet` | one element's ordered changed fields; the batch; `to_wire()` |
| `scene_push.py` | `InstallScene`, `PatchScene`, `NoPush` | *how to complete one push*, each knowing its own failure handling |
| `live_scene.py` | `LiveScene` | holds `_previous`; `plan(request) -> ScenePush`; `armed()` / `disarm()` |

**Why three and not an `if`.** The branches are not symmetric: `InstallScene`
awaits `show`; `PatchScene` awaits `update` and, on `OpError`, completes itself
by awaiting `show` (it holds the request for exactly that); `NoPush` awaits
nothing. Folding that into `LuxScenePublisher._publish` gives one 92-line module
two responsibilities and a three-way conditional with divergent error paths —
the shape `oo.md` §*Polymorphism Over Conditionals* names. `NoPush` is a
textbook Null Object (PY-DP-9) and the trigger is genuine: it is the common case.

With this, `LuxScenePublisher._publish` reads:

```python
push = self._live.plan(request)
result = await push.apply(self._ensure_client())
```

— no branch at all, and the publisher keeps its one job.

**Do not extract:**

- A patchable-field allowlist per element kind — §2.6, third note.
- A generic "scene differ framework". `SceneDiff` is one function's worth of
  work; put it on `LiveScene` as `plan`, where the previous tree already lives
  (PY-OO-7: a free `diff(a, b)` helper next to `LiveScene` would be a method in
  disguise).
- Anything in the panel's `VoxPanelService` (436 lines — already the largest
  module in `panel/`). It gains one `LiveScene` field and one method; the
  decision logic lives in `lux_common`, not here.

**Module sizes after** (all under the 300-line PY-OO-2 threshold): `scene_patch`
~70, `scene_push` ~90, `live_scene` ~110, `album_roster` ~55.
`now_playing_block.py` **shrinks** from 120 as its mode branch dissolves.

No migration, compat, shim, or dual-path code anywhere: `music.now` is deleted,
`AlbumDisplay.track_count` becomes `tracks`, `show`-on-refresh is removed, not
flagged off.

---

## 4. Tests the implementation must carry

Defect 1:

1. `AlbumDisplay.read` counts ready Parts, not manifest parts — an album minted
   with 0 snapshot parts and 5 on disk reports **5**.
2. `AlbumRoster.read` drops an album whose `store.open` raises `LookupError`,
   and `albums`/`displays` stay the same length.
3. `AlbumRoster.read` **propagates** `OSError` — the silent-vanish guard.
4. **Coherence**: for the playing album, the `Tracks` cell equals the `of` in the
   position line (§1.5). This is the regression test for the reported bug.
5. `AlbumListScene.render_request()` performs **zero** store reads — assert
   against a `ProgramStore` double that raises on `open`.

Defect 2:

1. Identical consecutive renders produce `NoPush` — zero client calls.
2. A track change produces exactly one patch, on `music.now.position`.
3. Idle→playing produces **patches, not an install** — the roster-invariance
   proof.
4. Play→pause patches `music.transport.playpause`'s `label`, `tooltip`, **and**
   `publish`.
5. **A table patch orders `rows` before `selected_row_ids`** — assert on the
   emitted wire list, not on observed behavior.
6. Active→idle emits `selected_row_ids: []` — the vanishing-field guard.
7. An `OpError` from `update` falls back to `show` with the same request.
8. `HubUnavailableError` disarms; the next push is a `show`.
9. A group's changed child patches the **child id**, never the group, and never
   emits `children`.
10. Panel: `acknowledge` installs; `service` and `repush` patch.
11. Panel: a provider change patches `vox.panel.model`'s `items` and `selected`
    in one batch.

## 5. Verification playbook (Phase 3)

`make check` cannot see this defect — both bugs are invisible to the type checker
and to any in-process test of the projection. The demo is the gate.

1. `make install`; `vox daemon restart` (the daemon serves old code until
   restarted).
2. `mic:music subcommand="on"` with 12 variations. Open **Music** from the lux
   menu. Watch the `Tracks` cell for the new album climb `0 → 1 → … → 12` as the
   fill lands, and confirm the cell equals the `M` in `N of M` once playing.
   *Today it stays `0`.*
3. Move the lux frame behind another window. Let two tracks change. **The frame
   must not come forward.** Confirm the position line updated behind it — use
   `lux inspect_scene vox.music` to read the Hub's tree without touching focus.
4. Click **Music** in the menu while the frame is buried. It **must** come
   forward — the `show` path is intact.
5. Bury the panel; change Notifications. The panel must not raise; the radio
   must move.
6. `vox daemon restart` with the scene open — the first refresh after the restart
   re-installs cleanly (the `OpError` fallback).
7. Operator confirms 2–6 by eye. Nothing here is judgeable from a log.

## 6. Noted, not fixed

`playback_notice.py:49` names a failed album `manifest.tags.name or f"album
{id}"`, while the table row shows `AlbumNames.friendly()` — which appends a
collision suffix (`Synthwave (a1b2c3)`). A play failure on a collided album
therefore names it differently from the row the user clicked. Same widget, two
names for one album. It is a one-line change (`AlbumNames(albums).friendly`),
the albums tuple is already in hand at that call site, and it is welcome as a
ride-along under the PR-purity rule. It is **not** part of either defect and I
have not folded it into the write set below; the implementer may take it or
leave it, but should not discover it as a surprise.

## 7. Write set for implementation

Sized for one implementation mission. `punt_lux` is untouched — every capability
this needs ships in 0.28.0.

### New

```text
src/punt_vox/lux_common/scene_patch.py
src/punt_vox/lux_common/scene_push.py
src/punt_vox/lux_common/live_scene.py
src/punt_vox/voxd/music_player/album_roster.py
tests/lux_common/test_scene_patch.py
tests/lux_common/test_scene_push.py
tests/lux_common/test_live_scene.py
tests/music_player/test_album_roster.py
```

### Modified — defect 1

```text
src/punt_vox/voxd/music_player/album_display.py   # tracks field + read(); drop track_count
src/punt_vox/voxd/music_player/album_table.py     # take AlbumRoster; _row reads the display
src/punt_vox/voxd/music_player/scene.py           # take AlbumRoster
src/punt_vox/voxd/music_player/player.py          # _submit reads the roster once
```

### Modified — defect 2

```text
src/punt_vox/lux_common/__init__.py               # export the three new classes
src/punt_vox/voxd/music_player/now_playing_block.py  # invariant 2-slot roster; drop music.now
src/punt_vox/voxd/music_player/album_table.py     # _selection always emits selected_row_ids
src/punt_vox/voxd/music_player/lux_scene_publisher.py # plan/apply; disarm on outage
src/punt_vox/voxd/music_player/scene_mailbox.py   # carry the sticky install intent
src/punt_vox/voxd/music_player/ports.py           # ScenePublisher: submit + reinstall
src/punt_vox/voxd/music_player/presenter_ports.py # ScenePresenter: the install verb
src/punt_vox/voxd/music_player/player.py          # reinstall() alongside notify_changed()
src/punt_vox/voxd/music_player/lux_subscription.py # on_connect/on_callback -> reinstall
src/punt_vox/panel/service.py                     # LiveScene; push_scene vs install_scene
src/punt_vox/panel/panel_guard.py                 # repush -> the patch path
```

**Tests updated** (existing files that assert on `music.now` or on
`show`-per-refresh)

```text
tests/music_player/test_album_display.py
tests/music_player/test_album_table.py
tests/music_player/test_scene.py
tests/music_player/test_player.py
tests/music_player/test_lux_scene_publisher.py
tests/music_player/test_lux_subscription.py
tests/music_player/test_scene_mailbox.py
tests/panel/test_service.py
tests/panel/test_panel_guard.py
tests/panel/test_panel_runner.py
```

### Docs

```text
CHANGELOG.md    # Fixed: live track counts; Changed: patch-based widget refresh
```

`docs/architecture.tex` needs no change — the daemon/client boundary is
unmoved. `DESIGN.md` warrants one ADR: *"widget refresh patches an installed
scene; `show` is reserved for install and for the two user gestures that mean
'bring this window to me'"* — the rejected alternative being "replace all `show`
with `update`", which breaks the menu entry (§2.1).
