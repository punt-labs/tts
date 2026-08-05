# vox-ig52 — Music Generation Resilience & Client-Observable Failure

**Status:** Spec (to implement next session). Author: Claude (COO). Bead: `vox-ig52`.
**Supersedes the original narrow framing** ("fill not recovering from transient
provider errors") with the correct, broader one surfaced 2026-07-05.

---

## 1. Problem

Background-music generation can fail, and when it does the failure is **invisible
to every client**. Observed live 2026-07-05:

- `/music on style "piano instrumental - classical style"` (vibe *sleepy*). The
  agent-authored prompt named composers (Chopin, Debussy, Satie, …).
- ElevenLabs Music rejected it: `400 bad_request`, `status: bad_prompt`,
  *"Your prompt appears to have violated our Terms of Service"* (it even returned
  a `prompt_suggestion` with the names stripped).
- `voxd` logged `ERROR punt_vox.voxd.music.loop: Music could not start; disabling`
  and raised `RuntimeError: could not generate the first track`.
- **The user saw the panel say "generating…" and then nothing.** No error, no
  reason, no state change. The only record was in `voxd-stderr.log`.

Two defects:

1. **Silent failure.** The daemon swallows the provider error, disables music, and
   tells no client. (Silent-failure-hunter territory.)
2. **Log-only observability.** The reason existed *only* in the daemon log. No MCP
   client, CLI caller, or user can read that log — and they must not have to.
   *Reading logs is not a strategy for software clients.*

A third, contributing input problem (cheap to fix independently): the `/music`
prompt-authoring guidance says "name forms/instruments/modes" but does not forbid
named artists/composers/titles, which is exactly what ElevenLabs Music's ToS
rejects.

## 2. Design principle (the north star)

> **Every state and failure a client cares about must be observable through the
> client API — the `status` MCP tool and the tool return values — never only in a
> log.**

This mirrors the org's own Phase-3 verification rule ("observe via the project's
introspection APIs", e.g. lux `list_errors`). Vox's introspection surface is the
`status` tool; today it reports `music_mode` but not *whether generation
succeeded or why it failed*. Closing that gap is the core of this work. The
acceptance test observes failures **through `status`**, not the log.

## 3. Music generation lifecycle — state machine (z-spec target)

Model this formally with `/z-spec:code2model` **before** implementation (it
qualifies: stateful subsystem, invariants across transitions, and a wrong
transition silently corrupts UX — which is exactly what happened). Produce
`docs/music-resilience.tex`, `fuzz -t` clean, ideally `/z-spec:test`
model-checked.

**States** (per `(vibe, style)` session):

- `off` — no music.
- `generating` — first track not yet ready (or fill in progress below full).
- `playing` — a track is playing; pool may still be filling.
- `rotating` — pool full (12); shuffling, zero credits.
- `retrying` — a generation call failed with a **transient** error; backoff timer
  active; music stays alive on whatever pool exists.
- `failed` — generation cannot proceed (permanent error, e.g. `bad_prompt`, or
  transient retries exhausted with an empty pool); music is not playing; a
  `last_error` reason is retained and queryable.

**Key invariants** (carry into the schema predicate):

- `last_error` is non-empty **iff** state ∈ {`failed`} (and optionally retained as
  advisory in `retrying`); it is cleared on the next successful generation or a
  fresh `music on`.
- `failed` is reachable from `generating`/`retrying`, never from `rotating`
  (a full pool cannot hard-fail — it plays from disk at zero credits).
- Entering `failed` **must** emit an observable transition (status flips +
  `last_error` set), never a silent disable.
- At most one fill task active (existing invariant, preserved).
- `retrying` only for transient errors; `bad_prompt`/auth go straight to `failed`
  (retrying a ToS-rejected prompt is pointless).

**Transitions to specify** (pre/post): `TurnOn`, `FirstTrackOk`, `FirstTrackBadPrompt`,
`FirstTrackTransient` (→ retrying), `RetryExhausted` (→ failed), `FillOk`,
`FillError(kind)`, `Recover` (transient clears → resume), `VibeStyleChange`,
`TurnOff`. Each must state its effect on `state` **and** `last_error` so the
observability contract is provable from the model.

## 4. Observability contract (client API)

**`status` tool gains:**

- `music_state`: one of `off | generating | playing | rotating | retrying | failed`.
- `music_last_error`: string reason when `failed`/`retrying`, else empty. For
  `bad_prompt`, include the human reason **and** the provider's `prompt_suggestion`
  so a calling agent can re-author without guessing.
- (Keep `music_mode` for back-compat, or fold it in — implementer's call, but do
  not remove observability.)

**`music` / `music_next` tool return values:**

- Synchronous failures (missing key, immediate reject) return an error result with
  the reason — not a cheerful "generating" panel.
- For async first-track failure, the tool may still return "generating", but the
  outcome becomes observable via `status` within one poll; a client that polls
  `status` sees `failed` + reason. (If the notification system is wired, also emit
  a spoken/panel notification on entering `failed` — nice-to-have, not required.)

**Non-goal:** clients never parse `voxd-stderr.log`. The log stays for operators
debugging the daemon; it is not a client interface.

## 5. Error taxonomy → behavior

| Provider signal | Class | Behavior |
|---|---|---|
| `400 bad_prompt` / ToS | permanent | → `failed`, `last_error` = reason + `prompt_suggestion`. No retry, pool not burned. Agent re-authors. |
| `401/403` / missing key | auth | → `failed`, `last_error` = "Background music requires a valid ElevenLabs API key". (partly done) |
| `429` / quota / `5xx` / timeout | transient | → `retrying`, bounded exponential backoff (cap N attempts). Keep playing existing pool. On success → resume; on exhaustion with empty pool → `failed`. |

## 6. Input guard (independent, do first — cheap)

Amend `commands/music.md`: an explicit prohibition — **never name specific
artists, composers, bands, or copyrighted song/piece titles** ("Chopin",
"Clair de lune", "in the style of X"); ElevenLabs Music rejects them under ToS.
Use **forms, instruments, modes/scales, eras, tempo, key, mood** instead
(the Klezmer worked-example already models this; add the classical-piano
counter-example — "nocturne in E-flat, rolling left-hand arpeggios" not
"Chopin nocturne"). This alone prevents the most likely `bad_prompt` trigger.

## 7. Acceptance criteria (verified through the API, not the log)

1. Drive a **deliberately bad** prompt (named composer) through the `music` tool →
   within one poll, the `status` tool returns `music_state = failed` and a
   `music_last_error` containing the provider reason + `prompt_suggestion`. **No
   log reading in the test.**
2. Simulate a transient error (mock 429) → `status` shows `retrying`, music keeps
   playing the existing pool, and recovery resumes fill on success.
3. A full (12) pool never enters `failed` on a provider hiccup — it rotates from
   disk.
4. `bad_prompt` does not disable/burn the pool; a corrected `music on` succeeds.
5. Modeled properties asserted **by name** in tests (e.g. `test_bad_prompt_sets_failed_state`,
   `test_transient_keeps_playing`, `test_full_pool_never_hard_fails`).
6. `make check` EXIT=0; live-verified by the COO through `status` before the bead
   closes — verified live before the bead closes, never on `make check` alone.

## 8. Execution plan (next session)

1. **Input guard now-ish:** COO edits `commands/music.md` (§6). Independent, doc-only.
2. **Design/model mission:** `jms` (Z) authors `docs/music-resilience.tex` from §3;
   `fuzz -t` clean. COO reviews the model's findings, escalates any before impl.
3. **Implementation mission:** `rmh` (owns `voxd` music + `server.py` status surface),
   evaluator `bwk`/`mdm`. Single agent, **no worktree**. Implements §4/§5 to satisfy
   the model; adds §7 tests. Commit-per-step.
4. **Live verify:** COO drives the bad-prompt and transient paths, observes via the
   `status` tool, confirms with operator, then closes `vox-ig52`.
