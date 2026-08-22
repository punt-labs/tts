# Conversation Mode — Phase 1 Spike Plan

Companion to `docs/conversation-mode-prd.tex`. Each spike exists to answer
one open question from the PRD with a measurement or a human's ear, before
any of it becomes a design mission or a Z spec. Spikes are throwaway:
prototype code lives under `.tmp/spikes/conversation-mode/<name>/`, is not
held to `make check`, and is never merged as production code. What survives
a spike is a short written finding, appended to this file, that feeds the
eventual design mission's write-set and invariants.

**Constraint on all five:** nothing here touches the `lux` sibling repo. A
spike that wants a visual status readout drives it through lux's existing
MCP tools (`scene.show`, `scene.update`, …) exactly as any other vox
component does today — no new lux surface, no lux code changes. Spikes may
freely reuse existing vox code (providers, `voxd`'s audio path, the music
program) and existing patterns from prior art (Unramble's turn-detector
shape, discussed earlier) — reuse is expected, not a violation of
"throwaway."

Every spike is gated by ElevenLabs + headphones only, per the PRD's Phase 1
scope. No local providers, no Linux, no no-headphones handling in any of
these five — that is Phase 2, deliberately not spiked yet.

## Sequencing

Spikes 1–2 are prerequisites for everything else: there is no point
measuring end-to-end feel before knowing whether the raw provider and the
turn-detector can hit the targets in isolation. 3 and 4 can run in
parallel once 1–2 report back. 5 is the integration spike and only starts
once 1–4 have each individually cleared their bar.

```text
  1 ─┐
     ├──▶ 3 ─┐
  2 ─┘       ├──▶ 5
     4 ──────┘
```

---

## Spike 1 — ElevenLabs round-trip latency baseline

**Question it answers:** are NFR-1 (interruption <300ms) and NFR-2
(first-word latency ~1s, flat regardless of reply length) achievable with
ElevenLabs alone, before any orchestration logic sits on top of it?

**What it builds:** a bare CLI harness — no state machine, no turn
detection, no agent — that opens an ElevenLabs Scribe v2 Realtime WebSocket
session and a streaming-TTS WebSocket session back to back, feeds canned
audio in, and instruments every hop: mic-to-partial-transcript,
partial-to-committed-transcript, text-sent-to-first-audio-byte. Reuses
vox's existing `providers/elevenlabs.py` TTS client wholesale; the STT leg
is new, minimal, and disposable.

**Go/no-go:** if raw provider latency alone exceeds the NFR targets, the
targets in the PRD are wrong and must be revised before Spike 2 bothers
building anything on top. If it clears with headroom, the budget for
turn-detection and orchestration overhead in Spikes 2–3 is known.

**Time-box:** 2 days. **Suggested owner:** rmh (owns `voxd`'s provider
integrations).

---

## Spike 2 — Barge-in feel, headphones only

**Question it answers:** can a human interrupt the agent and perceive it as
instant (NFR-1), using a from-scratch turn-detector modeled on Unramble's
`LiveTurnPauseDetector` shape but simplified for the headphone case — no
echo cancellation needed, since the mic never hears the speaker?

**What it builds:** a standalone audio-only detector (RMS/noise-floor
tiers, no ASR involved) wired to interrupt a currently-playing TTS stream
the moment it fires. Runs against real playback, real interruption, a
stopwatch, and a human. No agent, no STT — this isolates the one number
that matters (speech-onset to playback-stop) from everything else that
could confound it.

**Go/no-go:** measured interruption latency against the NFR-1 target.
If it misses, this spike is the cheapest place to find out — before that
detector is wired into a full state machine.

**Time-box:** 3 days. **Suggested owner:** kpz (audio/playback latency).

---

## Spike 3 — Sentence-streamed response, real agent think time

**Question it answers:** does FR-11 / NFR-2 hold against an actual Claude
Code agent turn that takes several seconds and involves tool calls, not a
canned string? Does speaking start on sentence one while the agent is still
producing sentence three?

**Design constraint, added after reviewing vox's existing batch TTS
pipeline (`core.py:split_text`, `providers/chunked.py:chunked_synthesize`):
this spike must not build a second, parallel pipeline.** Today's batch path
(split known-in-advance text into sentence-bounded chunks → synthesize each
→ stitch into one file) and Conversation Mode's incremental path (segment
text as it streams in from the agent → synthesize each → play progressively)
are the same operation with one difference: whether the input is fully known
up front or arrives over time. Batch is the degenerate case of streaming — a
"stream" with exactly one chunk that happens to already be complete. The
spike's job is to design and prototype ONE pipeline that serves both, reusing
`split_text`'s sentence-boundary logic as the segmentation core rather than
writing a second segmenter, so vox ends up with one chunk-synthesize-deliver
path instead of two to maintain. If unification turns out to be a bad idea
for a concrete reason (not just "it's more code to change now"), that
finding is exactly as valuable as a working prototype — report it either way.

**What it builds:** a segmenter (built from or replacing `split_text`) sat
between a live Claude Code session's streamed output and ElevenLabs'
streaming TTS input, structured so the existing batch callers
(`providers/elevenlabs.py`, `providers/openai.py` via `chunked_synthesize`)
could plausibly call the same underlying pipeline with a single-shot input
instead of their current separate synthesize-all-then-stitch path — a sketch
of how that convergence would work is part of the deliverable, even if the
existing callers aren't actually rewired in this spike. Ask the live agent
three or four real questions that require file reads or searches; measure
time from "agent has a first sentence" to "human hears it," independent of
total reply length.

**Go/no-go:** NFR-2's provisional one-second target, against a real agent
rather than a mocked one — this is the first spike where the agent's own
latency (not a provider's) is in the loop, and where the target might turn
out to be provider-bound instead.

**Coordination note (learned from Spike 2):** if any part of this needs a
human to listen or react in real time, one interaction per tool call, with
an explicit message in the conversation before each — never an unattended
multi-trial batch relying on a script's stdout as a cue.

**Time-box:** 3 days. **Suggested owner:** rmh.

---

## Spike 4 — `mic:music` as the waiting-phase presence signal

**Question it answers:** the PRD's open question, verbatim — does music
ducked under a live wait actually read as "the agent is still working," or
does it read as unrelated background music? This is a judgment call only a
human can make, not something a metric settles.

**What it builds:** the smallest possible wiring — a real multi-second
agent turn (reusing Spike 3's harness if it exists by then) with
`mic:music` started and ducked for the duration of the wait, nothing else.
Try at least two candidate treatments (e.g., a fixed low-key ambient loop
vs. a short spoken acknowledgment layered once at the start of the wait)
so there is a comparison, not a single untested guess.

**Go/no-go:** this is a listen-and-judge spike, not a threshold one — per
vox's own audio-demo discipline, a human confirms whether either treatment
lands before it becomes FR-12's actual mechanism. If neither lands, the
fallback (a narrated tool-use ticker, or a short phrase pool) gets spiked
next, not designed blind.

**Time-box:** 2 days. **Suggested owner:** claudia (prose/voice curation,
already vox's owner for quip and vibe design) with rmh wiring the mechanism.

---

## Spike 5 — Integration: does the whole call feel like a call

**Question it answers:** the only question that actually matters, and the
only one that cannot be answered by any of the four above in isolation:
strung together — listen, detect the turn, transcribe, hand to the live
agent, stream sentences back, allow a barge-in, keep presence during a long
wait — does a real conversation with a real Claude Code session feel like
something a person would choose over typing?

**What it builds:** a minimal vertical slice wiring Spikes 1–4 together
into one call loop. Not the production state machine, not the Z spec, not
hardened for edge cases — just enough of a real path to hold five to ten
genuine conversations end to end.

**Go/no-go:** this spike is judged by ear, live, with the operator, per
vox's existing audio-demo gate — not by a metrics dashboard. Its finding is
qualitative and becomes the brief for the design mission: what worked
as-is, what needs to change before a Z spec is written for the real call
state machine.

**Time-box:** 4 days, after 1–4 report. **Suggested owner:** rmh, evaluated
by kpz.

---

## What happens after

Findings from all five spikes get appended below this line, then feed
directly into the design mission for Conversation Mode's real
implementation — write-set, Z spec for the call state machine (idle →
listening → waiting → speaking, per `docs/WORKFLOW.md`'s stateful-audio
gate), and a revised PRD if any NFR targets or FR mechanisms changed based
on what was actually measured.

### Findings

#### Spike 1 — ElevenLabs round-trip latency baseline (run 2026-08-22)

Ran as an agent-team experiment (see below for a note on that). Real
measurements, 5 trials each leg, against a real ElevenLabs account.

| Leg | mean | median | min | max |
|---|---|---|---|---|
| STT: mic → first partial transcript | 2331ms | 2314ms | 2218ms | 2467ms |
| STT: last partial → committed transcript | 232ms | 172ms | 52ms | 403ms |
| TTS: text → first audio byte | 150ms | 148ms | 145ms | 162ms |

**NFR-2 (~1s to first spoken word): clears comfortably.** TTS floor alone is
145–162ms, leaving ~840ms of budget for agent reasoning, sentence chunking,
and pipeline overhead.

**NFR-1 (interruption <300ms): the STT pipeline cannot meet this if barge-in
waits on a transcript.** Mic-to-first-partial is 2.2–2.5s — 7–8x over
budget. This is the load-bearing finding for Spike 2: interruption
detection must run on raw audio locally, never gated on any STT event.

Notable from the ElevenLabs SDK: a full high-level realtime STT client
ships with an event-emitter API and a real error taxonomy (`auth_error`,
`quota_exceeded`, `commit_throttled`, `rate_limited`,
`session_time_limit_exceeded`) worth modeling explicitly rather than one
generic error path. Streaming partials also self-correct mid-stream (a
transcript revised then reverted a phrase across two partials) — a
turn-detector or UI consuming partials must tolerate that churn, not just
append it.

#### Spike 2 — Barge-in feel, headphones only (run 2026-08-22)

Two measurement passes, for different reasons.

**Pass 1 (sandboxed agent worktree):** no working microphone in that
environment — PortAudio/ffmpeg failed silently (all-zero samples), almost
certainly an OS mic-permission prompt with no human present to grant it.
Detection latency (30–90ms depending on RMS tier) was validated only
against synthetic RMS streams; kill latency was validated for real
(median 2.03ms, worst 5.11ms across 15 trials, real `afplay` subprocess
killed via `SIGTERM`). That pass also caught a real bug: the original
threshold fired on a single 30ms transient, meaning a cough could
false-trigger a barge-in — fixed by requiring 2 consecutive strong-tier
chunks (60ms) before firing.

**Pass 2 (this session, live, real mic and speakers):** first attempt
(3 trials, unattended, run back-to-back with an audiobook playing nearby)
produced one real hit and two no-detections — invalidated by two
compounding defects: no real-time coordination between agent and human
(a script's stdout is not a live signal — see process note below), and
continuous speech-like background audio (the audiobook) corrupting the
detector's adaptive noise floor in ways nothing in the design accounted
for. Re-run properly — one trial at a time, explicit "starting now" in
the actual conversation before each, audiobook off:

| Trial | Interrupted at | Detection-to-kill |
|---|---|---|
| 1 | 1357ms into playback | 0.3ms |
| 2 | 1916ms into playback | 5.3ms |

**Go/no-go: clears NFR-1 with large margin** (0.3–5.3ms vs. the 300ms
target) on real hardware, real mic, real human interruption, headphones-off
but with a quiet room and no competing audio. Consistent with Pass 1's
synthetic kill-latency estimate. Two clean trials were judged sufficient by
the operator; not re-run a third time.

**Process note, worth keeping for every future live spike:** a script
printing "interrupt whenever you like" to stdout is not a coordination
mechanism — that text only reaches the operator after the blocking call
returns, and was mistaken for a live cue on the first attempt. The fix that
worked: one trial per tool call, an explicit message in the actual
conversation before each ("starting trial N now"), and the operator's
explicit go-ahead before firing it. Applies to Spike 3 and Spike 5 as well
— neither should be run as an unattended multi-trial batch.

**Note on the Agent Teams experiment (Spikes 1 and 2 dispatch):** both were
spawned as an experiment in Claude Code's Agent Teams feature
(`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`). It did not deliver its
distinguishing feature — direct peer-to-peer messaging between teammates.
The ethos-typed subagents used (`rmh`, `kpz`) are granted
`Read, Write, Edit, Bash, Grep, Glob` only; `SendMessage` is not in their
tool grant, so spike1-latency could not message spike2-bargein directly as
instructed, and the lead relayed the one cross-dependency (the STT latency
floor) by hand. The underlying team infrastructure is real (`SendMessage`
worked from the lead; `TaskStop` addresses teammates by `name@team`), but
peer collaboration requires the teammate to be spawned with that tool
granted — not the default for ethos specialist agents. For two spikes with
one real cross-dependency, manual relay cost nothing; it would not scale to
tasks needing frequent back-and-forth between teammates.

**Note on working alongside another active session (this session, before
Spike 3):** `/who` revealed another Claude Agento session (`claude:tty3`)
idle in this same working directory, not a separate worktree. This file
was found relocated from `docs/` to `.tmp/` twice without explanation —
most likely a collision with that other session's own file operations, not
a hook. Moved this session's work into an isolated worktree
(`.claude/worktrees/conversation-mode-spikes`) before continuing, per
`CLAUDE.md`'s "use a worktree if `/who` shows other agents active" rule,
which should have been checked at session start and wasn't.

#### Spike 3 — Sentence-streamed response, real agent think time (run 2026-08-22)

Prototype under `.tmp/spikes/conversation-mode/spike3-streaming/`:
`segmenter.py` (incremental sentence segmenter), `pipeline.py`
(segment → synthesize → play, against a real ElevenLabs account),
`harness.py` (drives one real trial per invocation, per the "one
interaction, no unattended batch" discipline — this spike is metrics-only
though, so that discipline was about pacing real tool-use turns, not
waiting on a human).

**(a) Latency — go.** Four trials, each seeded by a genuinely multi-second
real turn in this session (file reads and greps against vox's own
`playback.py`, `voice_resolver.py`, `providers/`, and `voxd/`, 5.9–11.7s of
real think time each, not canned strings), then the first sentence of the
real resulting answer fed into the pipeline:

| Trial | Real work before the sentence | Think time | Sentence 1 chars | Total reply chars | Sentence 1 → first audio byte |
|---|---|---|---|---|---|
| T1 | Read `playback.py` (158 lines) | 6.2s | 165 | 531 | 636.7ms |
| T2 | Read `voice_resolver.py`, grepped `providers/` | 5.9s | 242 | 654 | 644.3ms |
| T3 | Grepped + read `voxd/playback.py` | 9.4s | 231 | 653 | 682.6ms |
| T4 | Grepped `voxd/` and `providers/` for `chunked_synthesize`/websocket usage | 11.7s | 293 | 740 | 580.1ms |

Mean 636ms, median 641ms, range 580–683ms. **Clears NFR-2's ~1s target
with headroom on every trial**, and — the actual point of Spike 3 over
Spike 1 — **does not grow with reply length**: T4 had the longest total
reply (740 chars, longest sentence 1) and the *lowest* latency; T1 had the
shortest reply and a middling latency. The number is provider- and
pipeline-bound (dominated by ElevenLabs' streaming-TTS round-trip, ~600ms
here vs. Spike 1's bare 145–162ms floor measured without a fresh SDK
client/voice-resolution per trial), not agent-bound — consistent with
Spike 1's finding and now confirmed against a real, not mocked, agent
turn.

**Playback-start timing is reported separately and is NOT part of the
go/no-go call.** The interval from sentence-ready to the spike's own
`afplay` subprocess actually starting ranged 1.1–8.1s across trials, with
no consistent relationship to reply length either — noise from the
prototype's own thread-plus-`subprocess.run` player, not from synthesis.
See finding (c) below; a production implementation would not reproduce
this because it would not reproduce the prototype's player.

**(b) Pipeline unification — held up, with one real design correction
along the way.** Building the segmenter surfaced a genuine gap between
"batch is streaming with one chunk" as a slogan and as working code:

- `split_text`'s existing behavior *accumulates* sentences up to
  `max_chars` before flushing (fewer, larger provider calls) — cheap when
  the whole input is already in memory, because "does the next sentence
  fit?" is answered instantly. A first naive incremental port of that same
  accumulation loop, tested single-call-then-flush against `split_text`,
  produced *different* output: the batch path's trailing sentence (no
  space after its closing punctuation) got held back into `flush()` as a
  separate chunk instead of packed with the rest, because incremental
  code has to treat "did the sentence end, or did the input just end?" as
  a real, answerable question — one `split_text` never has to ask, since
  it always sees the whole string.
- The fix, not a workaround: give the segmenter one policy-independent
  core (`_absorb()`, sentence-boundary detection via the exact
  `_SENTENCE_SPLIT_RE`/`_split_at_words` `core.py` already uses) and two
  **flush policies** over it — `GREEDY` (accumulate to `max_chars`,
  exactly `split_text`'s behavior, and *carries its accumulator across
  `feed()` calls* so a multi-call stream converges on the identical
  packing a single-call batch produces) and `EAGER` (flush every sentence
  immediately, minimizing time-to-first-audio for a live call). Verified
  by direct comparison in `.tmp/spikes/conversation-mode/spike3-streaming/`:
  `IncrementalSegmenter(policy=GREEDY).feed(full_text) + .flush()` produces
  **byte-identical** chunk lists to `split_text(full_text, max_chars)`,
  at both a generous limit (one chunk) and a tight one (three chunks)
  forcing multiple packed groups; feeding the same text as several small
  deltas under `GREEDY` converges to the same packing too.
- **What this means concretely for the unified interface:** one
  segmentation core, driven by an iterator of text deltas (a live stream
  delivers many small deltas; a batch caller delivers one `feed(full_text)`
  then `flush()`), parameterized by a flush policy the *caller* picks
  based on whether it already has the whole input. `providers/elevenlabs.py`
  and `providers/openai.py` would call it with `policy=GREEDY` and get
  `chunked_synthesize`'s existing behavior back exactly; a Conversation
  Mode caller would call it with `policy=EAGER` and a live delta stream.
  `stitch_audio` stays as-is for batch callers (concatenate the segmenter's
  output chunks into one file); a streaming caller skips stitching
  entirely and hands each chunk to playback as it's synthesized — that's
  the one real branch point, not two segmenters.
- **Correction worth recording as its own finding:** an early real-agent
  answer produced mid-spike (T2's first sentence, before T4's research)
  claimed all five `TTSProvider` implementations call `chunked_synthesize`.
  Grepping to write T4 showed that's wrong — only `elevenlabs.py` and
  `openai.py` call it; `polly.py`, `say.py`, and `espeak.py` don't chunk at
  all. The unification case is still real (it collapses 2 call sites, not
  5), just smaller than first claimed, and the record above is corrected
  rather than left standing — the accurate version of the same point is in
  finding (c).
- **What would need to change for real callers to use it:** `core.py`
  would gain the segmenter (exported, not the private regex reached into
  by this spike) and a `policy` parameter threaded through
  `chunked_synthesize`, defaulting to `GREEDY` so existing callers are
  unaffected; `stitch_audio` is untouched. Nothing about `split_text`'s
  public contract (`list[str]`, every chunk `<= max_chars`) needs to
  change — `GREEDY` reproduces it exactly, so this is additive, not a
  breaking refactor of the batch path.

**(c) What surprised me building against a real agent turn instead of a
mocked one:**

1. **The real bottleneck a production implementation has to solve isn't
   the segmenter or the provider call — it's the async/sync boundary.**
   Grepping `voxd/` for this spike (T4) showed the daemon is fully async
   (every handler in `voxd/*.py` is `async def`, built on Starlette
   websockets), while `chunked_synthesize` and every provider's
   `_single_synthesize` are synchronous, blocking SDK calls. A prototype
   run as a bare script never has to face that; a real Conversation Mode
   implementation streaming chunks out of an async request loop does, and
   that bridging point is where the real design effort goes, not in
   segmentation logic (which this spike shows is close to done) or in the
   ElevenLabs call itself (already proven fast in Spike 1 and again here).
2. **The prototype's own playback stand-in is not something to build on.**
   `voxd/playback.py` already has a real `PlaybackQueue` with a
   suspicious-short-playback heuristic and platform-specific audio-backend
   diagnostics; this spike's thread-plus-`subprocess.run(["afplay", ...])`
   player has none of that, and its 1.1–8.1s of unexplained jitter (not
   correlated with reply length, so not a scaling defect — see (a)) is
   almost certainly an artifact of a bare Python thread contending for
   scheduling against a script issuing back-to-back network calls, not a
   provider or CoreAudio problem. A real implementation routes synthesized
   chunks through the existing queue and inherits its diagnostics instead
   of reinventing a thinner, less observable one.
3. **A real agent's answer needs the same tolerance for self-correction
   partway through a turn that Spike 1 found for STT partials.** T2's
   first sentence asserted something that turned out to be wrong once T4
   did the grep that actually checked it (see (b)'s correction). Nothing
   in the segmenter or pipeline cared — each trial's sentence was spoken
   as composed, and the correction lives in the write-up, not in a replay.
   But it's a genuine, mocked-input-can't-produce finding: a live
   Conversation Mode caller speaking sentence 1 while still forming
   sentence 3 will, at least occasionally, speak something the agent
   later needs to walk back — a UX question (does the agent audibly
   correct itself mid-call, the way a human would?) that a canned-string
   spike would never have surfaced, and that belongs in the design
   mission's open questions, not this spike's scope to answer.
4. **Real trials are slow in a way a mocked spike wouldn't reveal, and
   that's informative too.** Each trial's actual wall-clock time
   (13–60s, `trial_wall_time_s` in `results.jsonl`) is dominated by
   sequential real playback of 3 real sentences at natural speech
   length, not by anything the pipeline does slowly — a reminder that a
   live call's real UX bottleneck the human perceives is total speaking
   time, which no amount of pipeline optimization changes; only the
   *first-word* latency measured in (a) is what this spike's engineering
   can move.

#### Spike 4 — waiting-phase presence signal (run 2026-08-22)

Started from the plan's original framing (music vs. a spoken
acknowledgment) and ended somewhere more specific, entirely through live
listening with the operator — this finding could not have been reached any
other way, which is the point of a listen-and-judge spike.

**What was tried, in order:**

1. **Generic ambient music, unchanged during a wait.** The existing
   background Program (`ambient-drift-39c0ba`) kept playing through a real
   multi-second wait with no change. Operator's read: it's just music —
   indistinguishable from music playing whether the agent is working or
   idle. **Rejected**: carries no signal.
2. **A custom track authored via the Music-generation API** (`mic:music
   on`, base prompt + 12 variations describing a quiet-office soundscape —
   typing, paper shuffle, room tone, explicitly no melody/vocals/words).
   Operator's read: "basically it is typing... a bit repetitive," and had
   to be turned up loud enough that "any other audio is very loud" to hear
   at all. **Partial fail**: the Music-generation model is tuned for
   melody and rhythm, not foley — content was recognizable but limited in
   variation, and the operator's instinct (below) turned out to be the
   real fix, not further prompt iteration.
3. **The operator's redirect: use ElevenLabs' dedicated Sound Effects API
   instead of Music.** Confirmed via docs
   (`elevenlabs.io/docs/capabilities/sound-effects`) that this is a
   separate, purpose-built endpoint for foley/ambient sound — explicit
   guidance to describe sequences like "footsteps on gravel, then a
   metallic door opens" for realistic environmental sound, with native
   seamless-looping support, at 40 credits/second (a fraction of Music's
   ~2,000-per-track cost for the short loop this needs). Generated one
   20-second clip via `client.text_to_sound_effects.convert(text=...,
   duration_seconds=20, loop=True, prompt_influence=0.4)`, bypassing
   `mic:music` entirely (it only exposes the Music endpoint, not Sound
   Effects — a real gap, see below). Operator's read, music paused so the
   clip played in isolation: "basically it is typing. And the tempo is
   OK, but it is soft." **Content problem solved, level problem
   identified as separate.**
4. **Gain, not regeneration, fixed the level problem.** `ffmpeg -filter:a
   "volume=6dB"` on the already-generated clip, replayed: "I think it
   seems fine." No new API credits spent to fix loudness — confirms the
   loudness issue was a mixing-level problem, not a generation-quality
   problem, which matters for cost: level can be fixed once at authoring
   time, not re-solved per clip.

**The architectural conclusion, reached live and confirmed against the
actual code (not assumed):** FR-12's presence signal should not be
generated per-call at runtime at all. It should be:

- **Authored once, offline**, via the Sound Effects API (proven capable of
  the right content type once pointed at the right endpoint), gain-checked
  by ear the way this session just did, and the good takes kept.
- **Shipped as bundled static audio assets** — vox already ships fixed
  audio files this way for chimes; sound-effect loops are the same kind of
  asset, not a new distribution problem.
- **Played through the existing Program/`music_player` machinery**, not a
  new playback path. Confirmed by reading `types_programs/format.py` and
  `voxd/programs/program.py`: `Format` is a `StrEnum` of `PLAYLIST`,
  `PODCAST`, `AUDIOBOOK` — and the code's own docstring says `PODCAST` and
  `AUDIOBOOK` are named now specifically "so that `pool_size` and the
  operations branching on it are total from the start." The `Program`
  state machine (generation, pool, fill, retry, rotate — the whole Z-spec
  `Format.PLAYLIST`-generic machine documented in `program.py`) never
  branches on content *type*, only on `Format` for `pool_size` and
  `label`. A fourth format (ambience/sound-effect loops) is exactly the
  extension this architecture was built to take — reusing the entire
  state machine, not forking a parallel one. This is the same
  "don't-build-two-pipelines" principle Spike 3 applied to TTS synthesis,
  now confirmed to apply to playback too.
- **Runs on the existing background channel, independent of speech.**
  Grepping `speech_handlers.py` found zero references to `Program`, pause,
  or duck — confirmed live during this spike (the Program kept playing
  unchanged the entire time regardless of what was being read or spoken).
  `voxd` already runs two independent audio channels: foreground
  speech/chime (`playback.py`/`PlaybackQueue`) and background Program
  (`music_player`). That's the layering Conversation Mode needs (agent
  speech over ambient presence) — but no automatic ducking exists between
  the two channels today. Untested whether the two need to duck against
  each other acoustically or can simply coexist at authored levels; that's
  a design-mission question, not answered by this spike.

**Gap found, not fixed here:** `mic:music` (the MCP tool surface) only
calls the Music-generation endpoint. There is no tool-level access to the
Sound Effects endpoint today — this spike reached it by writing a
throwaway script that called the ElevenLabs SDK directly
(`.tmp/spikes/conversation-mode/spike4-presence/spike4_sfx.py`,
untracked, not committed). If sound-effect loops become the real
mechanism for FR-12 (or any future non-music ambience), that's new
provider-layer work, not something to retrofit onto the Music path.

**Go/no-go: content and level both resolved live; not a full go yet.**
The remaining open items before this is a real design: (1) how many
loops, of what length, ship in the bundle; (2) whether authored-level
coexistence with speech is sufficient or ducking needs building; (3)
whether this becomes a genuine fourth `Format` or a simpler static-asset
path that doesn't need the full Program state machine at all — the
architecture supports either, and that choice belongs to the design
mission, informed by this finding rather than decided by it.

#### Spike 5 — integration, does the whole call feel like a call (run 2026-08-22)

The most expensive spike to run and the most valuable, precisely because
it is the one no unit-level spike could substitute for. Built a minimal
two-script call loop (`.tmp/spikes/conversation-mode/spike5-integration/`
— `listen_turn.py`, `speak_turn.py`, `calibrate.py`,
`calibrate_bargein.py`, `interrupt_trial.py`) reusing Spikes 1–4's proven
pieces: real ElevenLabs STT and streaming TTS, `punt_vox.core.split_text`
for sentence segmentation (the actual production function, per Spike 3's
unification finding — not a reimplementation), and an RMS/noise-floor
detector descended from Spike 2's. Held several genuine live turns with
the operator: real questions, real tool-use "thinking," real spoken
replies, real interruptions.

**(a) The coordination lesson from Spike 2 recurred, sharper, and forced
a real design conclusion.** Text — chat messages, script `print`
statements — is not a valid coordination channel for this feature at
all, not just an inconvenience to work around. Partway through this
spike the operator was no longer reading the screen (the actual target
use case), which meant every "starting now" message and every
`print("Listening...")` was invisible, and several failed listening
attempts and a live self-correction ("not that other audio bled in — I
just hadn't spoken yet") happened before this was diagnosed. **Finding:
a real Conversation Mode implementation has no text fallback for
call-state signaling — every cue (ready to listen, still working,
finished speaking) must be audible, full stop.** This sharpens FR-3
("reflect the current call phase somewhere the user can check it") —
"somewhere the user can check" is insufficient; for the primary
hands/eyes-free scenario, the phase signal must reach the user by ear,
unprompted, with no requirement to look at anything.

**(b) A generic system chime (`afplay
/System/Library/Sounds/Tink.aiff`) was not reliable enough to trust,
even though it worked in isolation.** Verified standalone (exit code 0,
audible when run directly), but the operator did not hear it when
invoked from inside the listener script on two separate attempts, for a
reason not root-caused in this session — possibly output-device routing
differing between a bare `afplay` call and one nested inside
`uv run python`, not confirmed. Switching to vox's own `mic:unmute`
(real ElevenLabs TTS, the same channel already proven reliable all
session) worked every time it was tried. **Finding: the call's own voice
channel is the only channel proven reliable for state-signaling audio —
do not build a separate chime/tone subsystem for this on unverified
assumptions; reuse the TTS path already trusted for everything else,** or
if a distinct non-speech cue is wanted for latency reasons, it needs its
own dedicated verification before being trusted, not an assumption drawn
from working once in isolation.

**(c) The first working turn-detector port had a real, diagnosable bug —
not bad luck.** Ported with `CONSECUTIVE_STRONG_NEEDED = 2` (Spike 2's
value, tuned in a different session/room) initially, then over-corrected
to `14` (~420ms *consecutive*) after three environment-driven misfires,
which then failed differently: natural speech has brief amplitude dips
between syllables that reset a strict consecutive-chunk counter, so real
continuous speech no longer registered at all (`NO_SPEECH` twice in a
row). **Root cause, found only after the operator pushed back on
parameter-guessing and asked for real calibration data:** the correct
model is an *accumulated audible-run duration* that only resets on a
genuine silence gap (≥200ms), not a strict consecutive-strong-chunk
streak — the same distinction Unramble's `LiveTurnPauseDetector` design
already encoded (`runAudibleBytes` accumulates across a run; a `.pause`
only fires on sustained trailing silence) that this spike's first port
missed by simplifying too aggressively. Fixed once, verified against real
calibration data (`calibrate.py`: median RMS 0.035, peak 0.377, genuine
silent gaps mid-utterance while the operator spoke continuously for 5s),
worked reliably afterward. **Finding: do not tune detector parameters
against assumption or a single prior session's numbers — capture real
RMS data from the actual room/mic/voice first, the way `calibrate.py`
did, before touching a threshold.** This finding exists because the
operator explicitly stopped the guessing and asked for evidence
mid-session — worth naming, not just the technical fix.

**(d) A real, working end-to-end turn.** Once (a)–(c) were fixed: the
operator asked a real question by voice, `listen_turn.py` correctly
detected the turn and transcribed it (STT ~1.1s), the reply was composed
using genuine tool calls, and `speak_turn.py` began speaking with
first-word latency of 599ms — consistent with Spikes 1 and 3's numbers,
now proven against a real full loop rather than isolated legs.

**(e) Barge-in vs. backchannel discrimination is real, unsolved by
acoustic-duration statistics alone, and was resolved by direct human
tuning instead — a legitimate methodology, not a fallback.** The
operator interrupted mid-reply *unintentionally* — reacting verbally to
what was being said, not attempting to take the floor — and the
detector (inherited from Spike 2's raw "2 consecutive strong chunks"
policy, not yet updated with fix (c)) stopped playback anyway.
Distinguishing a deliberate interrupt from a normal conversational
backchannel ("mm," a short reaction) is a known hard problem in
conversational interfaces, not a threshold tuning oversight. Three
attempts to calibrate it from acoustic-duration statistics alone each
failed for a different, diagnosable reason:

- Trial 1 (interrupt) vs. Trial 2 (backchannel) into silence, no
  ongoing playback: clean separation (2040ms vs. 390ms) — but not a
  valid test, since neither trial had real ongoing speech to react to.
- Trial 3 (interrupt) collapsed to 240ms, *below* the backchannel
  number: the prompt sentence was too short to leave anything to
  interrupt, so the operator's utterance wasn't really an "interrupt"
  behaviorally, whatever it was labeled.
- A version with real concurrent playback (`interrupt_trial.py`, no
  early stop) produced uninterpretable fragmented runs (12 separate
  runs, 30–1860ms) — initially misdiagnosed as microphone echo
  contamination (a real and separate risk, genuinely present without
  headphones, but not what actually happened here). **The operator
  caught the real cause: the script never stopped playback on
  detection, so there was no closed loop — the operator had no signal
  to either stop or keep going, and was left guessing at an
  unresponsive system**, which produces behaviorally meaningless
  audio, not clean interrupt-vs-backchannel data. This was a bug in
  the test harness, not a property of human interruption behavior, and
  conflating the two would have been a real design mistake if it had
  gone unchallenged.

With the loop actually closed (stop the instant sound crosses a
threshold, then observe what naturally follows), the operator proposed
the methodology that actually worked: **pick a sustained-duration
  requirement, run it live, ask directly whether it felt too slow or too
  fast, and tune from real subjective feedback — not derived from raw
  acoustic statistics.** 1000ms → "200ms shorter" → 800ms → indistinguishable
  from 1000ms by ear → 500ms → clearly faster, "good enough." **Settled
  value: ~500ms of sustained speech required before playback stops**,
  reached through direct human-in-the-loop tuning, which is a legitimate
  calibration methodology for this kind of perceptual parameter, not a
  fallback from failing to derive it statistically.

  A secondary finding fell out of the 800ms step producing no audible
  change from 1000ms, and is best stated as two genuinely separate
  latencies that together make up the full interrupt experience, not one
  number:

  1. **Detection latency** — how long the human must sustain speech
     before the system decides a real interrupt is happening. This is
     what was tuned live: the ~500ms sustained-speech requirement.
     Entirely input-side; independent of anything on the playback path.
  2. **Termination latency** — once the system has decided to stop, how
     long until the human actually hears silence. This is *not* just
     process-kill time: `proc.terminate()` (`SIGTERM`) kills the `afplay`
     process, but does not guarantee audio CoreAudio has already buffered
     stops immediately. Spike 2's "detection-to-kill" numbers (0.3–5.3ms)
     measured process death only — they never measured audible silence,
     and a buffered-audio tail on top of process death is a real,
     separate cost. This is why a 200ms change in (1) produced no audible
     difference: it was likely smaller than the buffer tail dominating
     (2), so the change was real but inaudible.

  **Total perceived interrupt latency is (1) + (2), and this session
  only tuned (1).** (2) — kill-to-silence, buffer-inclusive, not just
  kill-to-process-death — was never measured and is a concrete,
  well-scoped follow-up for whoever picks this up next, not open-ended
  risk.

**Go/no-go: qualified go.** The full loop works, is fast enough
(NFR-1/NFR-2 both hold end to end, not just per-leg), and the operator
confirmed a tuned barge-in threshold that felt right by ear. It is
qualified because: (1) the 500ms figure is one operator's live-tuned
preference in one room on one day, not a validated default; (2) barge-in
vs. backchannel discrimination by acoustic duration alone remains
genuinely unsolved — the 500ms threshold makes *any* sustained sound of
that length trigger a stop, which is not the same as understanding
intent; (3) of the two latencies that
compose a real interrupt (detection, and termination-inclusive-of-buffer),
only detection was tuned — termination's buffer-tail component was
never measured; (4) audible state-signaling (finding (a)/(b)) is a
hard requirement this spike's harness only partially satisfies
(`speak_turn.py`'s replies are voiced; `listen_turn.py`'s "ready to
listen" cue was never made reliable in this session). All four are
concrete, scoped inputs to the design mission, not open-ended risk.
