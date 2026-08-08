# Vox Control Panel — Lux Applet

**Status:** implemented (bead vox-mhwj) and locked. Design confirmed against
lux internals by the lux agent (2026-08-06) — see "Confirmed with the lux
agent" below. Built via `ethos mission m-2026-08-07-009` and its follow-ons
through eleven rounds of local review (code-reviewer + silent-failure-hunter),
landing on branch `feat/vox-panel`.

A menu-launched Lux app that lets the user toggle mic mode (chimes vs. voice),
set the notification level, and pick a voice — without a CLI or MCP call.
Explicitly **not** built the way the Music Player is: no daemon-owned scene, no
`voxd`-held Hub connection, no persistent playback state to keep live. This is
a settings surface, opened on demand, that reads current state once and acts
on a click.

## Layout

```text
┌─ Vox ────────────────────────────────────────────────────────┐
│                                                                │
│  Notifications                                                │
│   ( ) Off      (•) Normal      ( ) Continuous                 │
│                                                                │
│  ────────────────────────────────────────────────────────    │
│                                                                │
│  Mic Mode                                                     │
│   (•) Chimes only      ( ) Voice                               │
│                                                                │
│  ────────────────────────────────────────────────────────    │
│                                                                │
│  Voice            [ Aria                              ▾ ]    │
│                    ▶                                          │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

Three independent controls, no cross-control invariant and no state machine —
each is a standalone radio group / combo that reflects vox's current setting
and applies a new one on change. This is why the feature does **not** trigger
the formal-modeling gate (`docs/WORKFLOW.md` § Formal Modeling): there is no
multi-state subsystem with invariant-bearing transitions here, just three
idempotent set-and-confirm actions.

- **Notifications** — radio group, mirrors `mic:notify` (`y`/`n`/`c` → Normal /
  Off / Continuous).
- **Mic Mode** — radio group, mirrors `mic:speak` (`y`/`n` → Voice / Chimes
  only).
- **Voice** — combo populated from `mic:who`'s roster, plus a `▶` glyph
  button that plays a short line in the selected voice before committing (so
  picking a voice is try-before-you-set, not blind). No text label on the
  button — same convention as the Music Player transport row (self-evident
  glyph, tooltip on hover, not "▶ Preview").

## Why an applet, not a `voxd`-hosted app

The Music Player is daemon-owned because its state is genuinely live —
playback position changes on its own, and a click must reach the one process
that owns the audio device. This panel has neither property: settings only
change on a click, and every control here already has a CLI front door
(`vox notify`, `vox speak`/`unmute`/`mute` — hmm, need mode names,
`vox voice`/`vox voices`). Building it as a `voxd`-hosted scene would mean
re-deriving the menu-registration, session-lease, and click-dispatch
machinery `voxd`'s music-player legs already hand-rolled once
(`lux_menu.py`, `composition.py`, `lux_scene_publisher.py`) — a second,
parallel implementation of infrastructure lux already ships as a library.

lux's `punt_lux.applets` package (`AppletProgram`, `AppletService`,
`SessionClaim`, `AppletLeg`, `SessionWatch`, `AppletIdentity`) is exactly this
machinery, generalized — but per DES-063 (settled, see below) that *package*
is scoped to third-party software lux wraps (`lux-beads` is the first of a
collection covering "everything else"). vox's own copy of the same pattern
lives in vox's own repo, using `punt_lux.applets` as a **library dependency**,
the same way `applets/beads.py` uses it internally — just vendored into
`punt_vox`, not added to lux's `applets/` folder. vox's CLI is a command-line
front door in the same sense `bd` is — the applet framework doesn't care
whether the front door reads or mutates. Building `vox-panel` this way gets
the menu-registration/session-lease/click-handling for free and keeps `voxd`
unaware this UI exists, same as `voxd` is unaware `bd` exists.

## Architecture

```text
Claude Code session (repo enabled for vox)
  └─ session-start hook spawns:  vox-panel --session-pid <PPID>
       (mirrors lux-beads: nohup'd, one per session, dies with the session)

  VoxPanelApplet                                  (new, vox-owned)
    = AppletProgram(
        SessionClaim.for_session("vox-panel", pid),
        AppletLeg(identity.client, VoxPanelService()),
        SessionWatch(pid),
      )

  VoxPanelService : AppletService                  (new, vox-owned)
    callback_id = "vox-panel"
    label       = "Vox"
    prefetch()    -- pre-connect VoxClientSync, cache current notify/speak/
                     voice state so the first click pays no connect latency
    acknowledge() -- push the cached/last-known state instantly
    service()     -- fetch authoritative state (VoxClientSync -> voxd) and
                     push the full control-panel scene via LuxRestClient

  In-scene controls carry the existing music-player `publish` pattern:
  each radio/combo change publishes a topic (`vox.notify`, `vox.speak`,
  `vox.voice`) the applet subscribes to via LuxHubClient.subscribe(*topics)
  BEFORE calling listen() -- the same WebSocket that carries the applet's own
  menu-click frames also carries these subscribed events (one _dispatch loop
  over CallbackFrame and EventFrame; confirmed against hub_client.py, not
  inferred). A received change calls the matching VoxClientSync method
  against voxd, then re-runs service() to push the confirmed state back.

  voxd is untouched: no new wire method, no new daemon-side lux client. It
  answers the same notify/speak/voice RPCs the CLI and MCP tool already call.
```

## Confirmed with the lux agent (2026-08-06)

1. **Single channel, confirmed against `hub_client.py`.** `LuxHubClient`
   (which `AppletLeg` wraps) dispatches `CallbackFrame` and `EventFrame` over
   the same receive loop, same WebSocket. No separate scene-interaction leg
   to open — subscribe to the topic a control's Hub-side handler publishes
   (`LuxHubClient.subscribe(*topics)` before `listen()`) and it arrives on the
   connection already carrying menu clicks. Beads has no precedent for this
   (it never publishes), so this applet would be the first consumer of that
   path from an applet rather than a daemon — not a new mechanism, just a new
   caller of an existing one.
2. **DES-063 (settled, DESIGN.md, operator-ruled 2026-08-01), point 3,
   verbatim: "Tools Punt Labs builds ship their own applet in their own repo
   (vox's music player is the reference); the collection covers everything
   else [non-Punt-Labs-built]."** So `punt_lux.applets` as a *package* is
   scoped to third-party wrapping (beads, future candidates); the *pattern*
   (`AppletIdentity`/`AppletLeg`/`SessionClaim`/`SessionWatch`/`AppletProgram`)
   is the general mechanism and DES-063 explicitly expects vox to use it —
   living in vox's own repo, imported as a library, not added to lux's
   `applets/` folder. The design above already reflects this.
3. **Session-start spawn — no existing vox precedent.** vox's own
   `hooks/session-start.sh` has no lux-spawn logic today: the Music Player
   never needed one, because it is daemon-hosted and starts with `voxd`
   itself, independent of any Claude Code session. So `vox-panel`'s
   session-start block is genuinely new for vox, modeled directly on lux's
   own `lux-beads` block (`pgrep -f "lux-beads --session-pid ${PPID}\$"` guard,
   `nohup lux-beads --session-pid "$PPID" ... &`) — same pgrep-guard-then-nohup
   shape, gated additionally on `.punt-labs/vox/enabled` the way vox's other
   hooks already are.
4. **Menu label — confirmed, and it changes the naming plan.**
   `AppletIdentity.for_session` derives the identity purely from the
   repository root and session pid (`ClientIdentity(kind="applet",
   name="lux · <repo> · #<session>", repo=repo.declared_path, ...)`); the
   *rendered* menu label is the repo directory's basename ("vox"), not a
   custom string a caller supplies — same mechanism confirmed independently
   for z-spec's applet today. voxd's Music Player entry is a separate
   `kind=app` machine-global daemon identity with no `repo` set, so no
   collision there. If vox ever ships a second repo-scoped applet alongside
   this one, both render "vox" and get numbered — DES-06x's intended
   disambiguation (Details panel distinguishes them), not a bug to design
   around. **Naming implication:** there is no `vox-panel`-specific menu
   label to bikeshed — the entry reads "vox" regardless of what the console
   script or `AppletService.label` is named; `label`/`callback_id` only need
   to be internally coherent (e.g. `vox-panel` console script,
   `callback_id="vox-panel"`).

## Build

Implemented on `feat/vox-panel` (`src/punt_vox/panel/`, `tests/panel/`, a new
`vox-panel` console script, and a `hooks/session-start.sh` spawn block) across
eleven review-and-fix rounds under mission `m-2026-08-07-009` and its
follow-ons. See vox-mhwj and vox-rols (a tracked follow-up to extract the
`PanelNotice`/`PlaybackNotice` and `HubOutageLog` patterns this feature
introduced into shared modules with the Music Player, not required for this
feature's own merge) and vox-tqpq (a pre-existing infinite-loop bug the review
found copy-pasted into 7 other hook scripts, tracked separately since none of
them are touched by this PR).

`VoxPanelLeg` decomposed under review into four collaborating classes as the
review found gaps in its failure handling: `PanelGuard` (owns the
outage-vs-refusal swallowing logic — `outage()` for a transient luxd drop,
`rejection()`/`offscreen_rejection()` for a real voxd refusal), `PanelRunner`
(the three pieces of work the leg starts and never awaits: the connect-time
warm-up, a menu click, a subscribed control-change), and `PanelMenuEntry`
(the menu-registration call and its own failure boundary — extracted because
it runs inside `on_connect`, which the Hub client isolates from the
connection's own retry loop, so a registration failure needs its own guard
rather than relying on `_listen_once`'s). `VoxPanelLeg` itself now only owns
the connection lifecycle and dispatch. Review also found and fixed a
`ConfigValueError`/malformed-payload `ValueError` conflation in
`src/punt_vox/frontmatter.py` that let a voice name containing a quote
silently revert with no on-screen notice — the fix extracted a new
`src/punt_vox/frontmatter_block.py` module for the frontmatter grammar,
paying down `frontmatter.py`'s own OO ratchet debt as a side effect — and
three separate `set -e`/infinite-loop bugs in `hooks/session-start.sh`'s new
spawn block (a non-git `cwd`, malformed stdin, an unreadable stdin fd, and a
relative `cwd` all aborting or hanging the hook before the panel could ever
spawn).

**Frame sizing (confirmed with the lux agent, 2026-08-07):** lux's default
frame size, when `frame_size` isn't passed, is a fixed 75% of the window's
content region (`display/server.py`, `_FRAME_FILL = 0.75`) — not derived from
the scene's actual content. `frame_flags: {"auto_resize": true}` fixes the
*height* but cannot fix the *width* here: this panel's section labels use
`style="heading"`, which `text_renderer.py` routes through ImGui's
`separator_text` widget — a labeled divider whose correct, by-design behavior
is to span the full available width (same as a plain `separator`). That
intentional full-width element feeds back into `auto_resize`'s size
computation, so the frame cannot converge narrower regardless of how compact
the actual controls are. This is **not a lux bug** — a `separator_text` that
didn't span the panel would look broken.

The fix for a static settings panel like this one: **skip `auto_resize`
entirely and pass an explicit `frame_size`** sized to the real content — from
`inspect_scene(want_geometry=true)` on the mockup, the widest row (the Voice
combo, 288px, plus the inline preview button) needs roughly 330–350px wide by
~220–260px tall; `vox-panel`'s actual `show`/`update` calls should use a
fixed `frame_size` in that range rather than any `frame_flags.auto_resize`.
`auto_resize` remains the right tool for content whose size genuinely varies
(tables, lists) — just not for this panel's static heading+separator layout.

**Caveat, confirmed by direct reproduction:** `frame_size` only takes effect
on a frame's **first** creation. Re-pushing to an existing `frame_id` with a
different `frame_size` is silently ignored — verified by re-pushing the same
scene to an already-created `frame_id` with `frame_size:[340,240]` and
getting the unchanged prior geometry back, then creating a fresh `frame_id`
with identical elements and the same `frame_size` and getting the correct
result. `vox-panel`'s applet must pick its `frame_id`'s size correctly on
first registration — it cannot rely on a later `show`/`update` call to
correct a wrong initial size.
