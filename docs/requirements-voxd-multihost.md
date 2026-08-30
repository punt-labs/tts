# Requirements: voxd Host-Adapter Contract and opencode Support

**Status:** Draft for review — not yet a design
**Date:** 2026-08-29
**Tracking:** Epic vox-o718
**Provenance:** Operator rulings and research session of 2026-08-29.
**Sibling document:** `biff/docs/requirements-biffd-multihost.md` — the
same program shape for biff, where a daemon must first be extracted.
Vox already has its daemon; this document is correspondingly smaller.
**Relationship to DESIGN.md:** This document states *what* the work
must achieve. Design missions produce the *how* as DES entries. Where
this document names a mechanism (`session.idle`, the SDK client), that
mechanism is a verified host capability — a constraint, not a design
choice left open.

Smaller scope than biff's program — the **same engineering bar**. This
is not a pilot hack: what ships here becomes the org template for
"tool = daemon + N host adapters," and every requirement below goes
through the full lifecycle (design mission, tests, Phase-3
verification through a real entry point, operator confirmation).

---

## 1. Overview

Vox is already the shape biff is being restructured into: `voxd` owns
synthesis, the playback queue, dedup, cache, recordings, and the music
program (DES-028); the MCP server, hook scripts, and CLI are thin
WebSocket clients (DES-017). What vox lacks is a *stated contract* for
what a host adapter is — the Claude Code surface is currently the only
adapter, so its habits and the contract are indistinguishable.

This program does two things:

1. **M1** states the host-adapter contract explicitly, so a second
   adapter has a definition to conform to and the first adapter can be
   audited against it.
2. **M2–M5** add an opencode adapter conforming to that contract:
   an npm plugin replacing the Claude Code hook layer, native tools
   replacing the `mic` MCP surface, enablement deposits, and a third
   release channel.

| Module | Name | Depends on |
|--------|------|------------|
| M1 | Host-adapter contract | — |
| M2 | opencode adapter: notifications | M1 |
| M3 | opencode adapter: tool surface | M1 |
| M4 | Enablement and deposits | M2, M3 |
| M5 | Distribution and release | M2, M3 |

Requirement keywords MUST, SHOULD, and MAY follow RFC 2119. Every
requirement carries an ID for traceability into design docs, beads,
and mission contracts.

---

## 2. M1 — Host-adapter contract

The invariants that make a second host possible. These MUST be stated
in DESIGN.md as a DES entry and enforced in review on every adapter
change, for every current and future host.

### Requirements

- **R-C.1** A host adapter contains *delivery mechanics only*: how a
  notification, tool call, or command reaches this host's human and
  model. Zero engine logic — no synthesis, no playback, no provider
  selection, no config interpretation may live in an adapter.
- **R-C.2** All engine access goes through voxd's existing client
  protocol (WebSocket, per DES-028). An adapter MUST NOT play audio,
  touch the config files, or reach a TTS provider directly.
- **R-C.3** Semantics are host-independent: notify levels (`y`
  completion + permission, `c` continuous), vibe behavior, dedup,
  per-repo enablement, and config ownership MUST mean the same thing
  on every host. Adapters MAY differ only in *how* a semantic event is
  delivered, never in *what* it means.
- **R-C.4** `.punt-labs/vox/enabled` remains the single, host-agnostic
  per-repo gate. One marker governs all hosts; enabling vox for a repo
  enables it for every installed host surface.
- **R-C.5** The Claude Code adapter (plugin hooks, `mic` MCP server,
  commands) is regression-free under this program: its behavior,
  tests, and release channels are unchanged except where an audited
  contract violation (R-C.1/R-C.2) is found — which is then fixed, not
  grandfathered.
- **R-C.6** Multiple adapters MUST be able to run concurrently against
  one voxd (a Claude Code session and an opencode session on the same
  machine at the same time), with dedup and queue serialization
  handled where they already live — in the daemon.

### Acceptance

M1 is done when the contract is a merged DES entry, the Claude Code
adapter has been audited against it with findings fixed, and the
opencode design mission cites the contract as its governing document.

---

## 3. M2 — opencode adapter: notifications

The opencode replacement for the Claude Code hook layer
(`plugin/hooks/`). One npm-distributed opencode plugin subscribing to
host events and calling voxd.

### Requirements

- **R-N.1** The adapter MUST deliver the same semantic notifications
  the Claude Code hook layer delivers, mapped to opencode events:

  | Semantic event | Claude Code today | opencode source |
  |----------------|-------------------|-----------------|
  | task completion | Stop hook | `session.idle` |
  | permission prompt | Notification hook | `permission.asked` |
  | error | hook layer | `session.error` |
  | post-compaction vibe refresh | PreCompact/session hooks | `session.compacted` |
  | continuous-mode signals | PostToolUse et al. | `tool.execute.after`, `file.edited` |

  The exact event mapping is design-confirmed against opencode source
  (SP-V.2), but the semantic set is fixed: nothing the Claude Code
  user hears goes missing on opencode, and nothing new is invented
  without a matching semantic on both hosts.
- **R-N.2** Notify levels MUST behave identically to Claude Code:
  level `y` delivers completion + permission events only; level `c`
  additionally delivers continuous signals; chimes-only vs spoken
  follows the same `speak` setting. Level state lives in voxd-owned
  config (R-C.2); the adapter only asks "what do I deliver?"
- **R-N.3** Task-completion narration MUST NOT require a model turn.
  DES-001's decision-block protocol exists solely because Claude Code
  hooks cannot read the transcript; opencode's plugin holds an SDK
  client that can. The adapter reads the completed session's last
  assistant message(s) via the SDK and submits recap text to voxd.
  The blocking pattern MUST NOT be ported.
- **R-N.4** Recap generation is a real design deliverable, not an
  afterthought: the design mission MUST specify the summarization
  approach (heuristic extraction vs an SDK model call), its quality
  bar relative to today's model-authored recaps, its latency budget,
  and its cost (a model call is not free; a heuristic is not good by
  default). The choice and its rejected alternative are logged as a
  DES entry.
- **R-N.5** Exactly-once semantics MUST match today's guarantees: one
  completion notification per task completion (DES-001's
  `stop_hook_active` loop-guard has an adapter-side equivalent if the
  host can re-fire `session.idle`), and daemon-side dedup (DES-028)
  continues to apply across concurrent sessions and hosts.
- **R-N.6** The adapter MUST NOT poll anything — voxd, the host, or
  the filesystem. It is event-driven on both sides: host events in,
  voxd calls out.
- **R-N.7** Adapter failures MUST be inert to the coding session: a
  voxd outage or adapter crash MUST NOT block, delay, or error the
  user's session. Silence is the failure mode, and failures are
  logged to voxd's existing log (never swallowed silently — inert to
  the session, visible in the log).

### Acceptance

M2 is done when a live opencode session audibly chimes and narrates on
task completion, chimes on permission prompts, honors `y` vs `c`, and
a voxd kill mid-session produces silence plus a log line — all
demonstrated through the real entry point and operator-confirmed.

---

## 4. M3 — opencode adapter: tool surface

The opencode replacement for the `mic` MCP server: native opencode
tools backed by voxd.

### Requirements

- **R-T.1** The full `mic` tool surface MUST be available as native
  opencode tools with argument and output parity: `unmute`, `model`,
  `provider`, `voice`, `speak`, `notify`, `vibe`, `status`, `rec`
  (all subcommands), `music` (all subcommands), and `enablement`.
  Host-specific display conventions MAY differ; capabilities and
  semantics MUST NOT.
- **R-T.2** Tool implementations MUST be thin voxd clients (R-C.1):
  the tool layer validates arguments, calls voxd, and renders the
  reply. Any logic beyond that is a contract violation.
- **R-T.3** Behavioral conventions from the agent guide carry over
  where the host allows: control actions silent on success, data
  actions report, `{"error": ...}` envelopes always reported. Where
  opencode's rendering makes a convention impossible, the deviation is
  documented in the deposited agent guide, not silently improvised.
- **R-T.4** Slash-command equivalents of the Claude Code commands
  (`/unmute`, `/mute`, `/vibe`, `/music`, `/vox:*` pickers) MUST ship
  in opencode's command format, routing to the native tools.

### Acceptance

M3 is done when every `mic` tool has a passing parity test against its
opencode counterpart (same voxd calls issued for the same inputs) and
the commands invoke them end-to-end in a live session.

---

## 5. M4 — Enablement and deposits

`vox enable` learns opencode; the marker stays singular.

### Requirements

- **R-E.1** `vox enable` MUST detect installed host surfaces and
  deposit what each needs. For opencode: the tools shim, the command
  files, the `opencode.json` plugin entry, and the agent guide — wired
  so opencode actually loads it, meaning referenced from `AGENTS.md`
  or listed in `opencode.json` `instructions`, since opencode does not
  parse the `@`-imports Claude Code uses. For Claude Code: unchanged.
- **R-E.2** `vox disable` MUST remove exactly what enable deposited,
  per host, leaving the `.punt-labs/vox/` subtree dormant as today.
- **R-E.3** Enable/disable MUST remain idempotent and MUST NOT run
  git, matching current behavior — deposits are committed via PR by
  the human.
- **R-E.4** A repo with both hosts enabled MUST behave correctly in
  both, simultaneously (R-C.6), from the one marker (R-C.4).

### Acceptance

M4 is done when enable/disable round-trips cleanly on a repo used from
both hosts, verified by driving both sessions.

---

## 6. M5 — Distribution and release

### Requirements

- **R-R.1** The opencode plugin ships as an npm package. It is a
  release channel of this repo, version-locked to the PyPI and
  marketplace channels: all channels ship together on every version
  bump, extending the existing two-channel rule.
- **R-R.2** CI MUST build and test the adapter (Bun/TypeScript
  toolchain) including at least one end-to-end test in which a real
  opencode session produces a voxd-played notification.
- **R-R.3** The plugin MUST pin or bound its `@opencode-ai/plugin`
  dependency and CI MUST catch upstream breakage before users do
  (opencode moves fast; a floating dependency is a support incident
  waiting to happen).
- **R-R.4** README, the deposited agent guide, and CHANGELOG document
  the opencode surface per the repo's documentation discipline.

### Acceptance

M5 is done when a clean machine can `vox install`, enable a repo, open
opencode, and hear vox — with all three channels at one version.

---

## 7. Spikes (resolve before design closes)

Source-level investigations against opencode; shared findings with the
biff program where noted.

- **SP-V.1** SDK transcript access: exact API for reading the
  completed turn's assistant messages from a plugin, its cost, and
  its behavior after compaction. Gates R-N.3/R-N.4. (Related to biff
  SP-2, different API surface.)
- **SP-V.2** Event semantics: does `session.idle` fire exactly once
  per completion? Does `permission.asked` fire for every permission
  prompt the TUI shows? What fires on error? Gates R-N.1/R-N.5.
- **SP-V.3** Plugin lifecycle: process model, restart/crash behavior,
  whether one plugin instance serves many sessions — decides where
  the voxd WebSocket connection lives. (Shared with biff SP-4 —
  run once, cite in both programs.)

---

## 8. Non-goals

- **Changing voxd, providers, playback, recordings, music, or config
  ownership.** The engine is untouched; this program is adapters only.
- **Porting the Claude Code adapter to the new mechanism.** DES-001's
  block protocol stays on Claude Code until that host offers
  transcript access to hooks; parity of *semantics*, not of
  implementation.
- **Other punt-labs tools on opencode.** Tracked by their own repos'
  epics (biff: biff-00v); this document is vox only — but M1's
  contract is written to be the org template.
- **A vox marketplace analog for opencode.** Distribution is npm plus
  enable-deposits; no bespoke installer surface.

---

## 9. Sequencing and delivery

1. **Spikes** SP-V.1–SP-V.3 (SP-V.3 shared with biff — run once).
2. **M1 contract** — DES entry + Claude Code adapter audit.
3. **M2 + M3** — the adapter, one design mission, implementation
   with commit-per-step.
4. **M4 enablement**, then **M5 release**.

Each stage is an ethos mission with this document's requirement IDs in
the contract criteria. Phase-3 verification for every user-facing
stage means a live opencode session, audible output, and operator
confirmation — `make check` alone does not close anything here.
