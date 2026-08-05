# Audio Programs — a unified multi-part audio concept (playlist / podcast / audiobook)

**Status:** High-level concept for review. Author: Claude (COO), 2026-07-05.
Not a design — the input to a design session. Bead: epic (see end).

## The idea in one sentence

Generalize the music playlist into one abstraction — a **Program**: a named,
addressable, ordered-or-rotating collection of **Parts**, each Part being
LLM-authored content + generated audio + metadata — with three **formats**
(playlist / podcast / audiobook) that differ only in which ElevenLabs engine
generates a Part, how Parts are ordered and played, and whether audio is a ducked
background or the foreground.

## Why one feature, not three

Playlist, podcast, and audiobook share the *entire* lifecycle and economics:

1. **Author** (LLM) → 2. **generate in the background** (ElevenLabs, costs credits)
→ 3. **store** as an ordered set of parts → 4. **play / advance / rotate**
(free — ElevenLabs charges for generation, not playback).

They also share the create/consume split, the daemon's playback queue, and the
"the LLM knows the content, vox is a pipe to ElevenLabs" principle we already
proved with music prompts. The differences are a clean **format strategy** axis,
not three separate subsystems.

## The Program model (fixes "it's a naming pattern, not a list" — vox-us4g)

- **Program**: `name`, `format` (playlist | podcast | audiobook), a subject
  (vibe+style for music; topic/brief for spoken), an ordered list of Parts, a
  **playback policy**, and a lifecycle state. First-class and persisted (a
  directory + manifest), so **CLI, MCP, and the daemon all address the same
  entity** instead of inferring a pool from filenames.
- **Part**: `index`, `title`, the authored spec (music prompt | dialogue turns |
  chapter text + voice casting), the generated audio file, `duration`, and
  `status` (pending | generating | ready | failed + reason).

The existing music internals generalize directly:
`TrackStore → PartStore`, `Playlist → Program` (with a playback policy),
`PoolFiller → Producer` (with a per-format generation backend). Reuse, not rewrite.

## The three formats

| Axis | **Playlist** (music) | **Podcast** (spoken series) | **Audiobook** (dramatic) |
|---|---|---|---|
| ElevenLabs engine | Music API `POST /v1/music` (`music_v2`, `force_instrumental`, 3s–10min, or a section `composition_plan`) | Text to Dialogue `POST /v1/text-to-dialogue` (eleven v3, multi-speaker turns, audio tags) | TTS long-form (`multilingual_v2`; Flash/Turbo up to 40k chars) + Dialogue for character lines + optional SFX/music bed |
| Part = | track | episode / segment | chapter |
| Order & lifecycle | shuffle-rotate, endless (fill to 12 then rotate) | sequential, finite N | sequential, finite chapters |
| Audio | background, **ducked**, loop | foreground | foreground (optional music/SFX bed) |
| Voices | none (instrumental) | cast: host + guest(s) | narrator + per-character cast |
| LLM authors | 12 genre prompts | a script (turns per speaker) | chapter text + voice casting |

Content is **orthogonal to format**:

- **Educational / project**: "teach me the ElevenLabs API", "walk me through
  PEP 8", "explain this repo's architecture" → a two-host **podcast** or a
  narrated **audiobook**; the LLM authors from its knowledge + the codebase
  (quarry/repo).
- **Entertainment**: "a 10-minute mystery loosely off my codebase" → a dramatic
  **audiobook** (multi-voice + SFX/music bed).
- **Ambient**: the music **playlist**.
- **Language learning**: content in a *target language* at a *target proficiency*
  (CEFR A1–C2) — e.g. "a B1-level German audiobook with a storyline about X", "an
  A2 Spanish podcast about my week." Applies to **podcast and audiobook**; the LLM
  authors the script/chapters directly in the target language at the requested
  level. ElevenLabs v3 and `multilingual_v2` cover 70+ languages, so it's a native
  fit. (Music is instrumental → not applicable.)

**Content dimensions** are orthogonal to format and are authoring inputs the LLM
receives per program: topic/brief, register (educational vs entertainment), and —
for spoken formats — **language + proficiency level**. Persistence is not a
dimension: **every Program is saved to disk (a named directory + manifest) and is
replayable** — from the user's point of view programs are permanent, not
throwaway. The contrast with Studio (below) is about *who authors and where
playback lives*, never about whether ours are saved.

## Who writes the content vs who runs the engine (revised 2026-07-25)

The real split is **who writes the content** vs **who runs the ElevenLabs
engine**. Writing the content is an LLM job; running the engine is the daemon's
job. One data type connects them — the authored input the LLM writes and the
daemon generates from:

- **The LLM writes the input.** For each format it produces one input object —
  music: a `base_prompt` + 12 variations; podcast: the speaker turns + which
  voice says each; audiobook: the chapters + the character voices. The MCP tool
  hands the LLM the schema and the prompt to fill in (as `commands/music.md`
  does today).
- **The CLI takes that same structured input.** It needs no LLM of its own —
  only the input in the right shape. A model, a human, or a script can hand it
  the structure and it works identically. `vox music new "<prompt>"` already
  does exactly this: the CLI passes a structured prompt to the daemon, which
  generates. "The CLI cannot generate" was never true.
- **Generation is daemon-side.** `voxd` holds the ElevenLabs key and runs the
  engine for whichever surface fed it structured input. Playback is free
  (ElevenLabs charges for generation, not playback), so consuming an existing
  Program costs nothing on either surface.

So the CLI is **not** consume-only and needs **no separate key**: it builds the
same input object the MCP tool builds and sends it to the daemon. This replaces
the earlier "CLI = consumption only until it gets its own keys" framing.

### One input type per format, built by both the CLI and the MCP tool

Each format has one input type for its authored content. Both the CLI and the
MCP tool build that same type and send it to the daemon:

| Format | Structured input (the authored content) |
|---|---|
| playlist | `base_prompt` + 12 genre variations |
| podcast | ordered `(speaker, text, audio_tags)` turns + a voice cast |
| audiobook | chapters (title + text) + character casting (+ optional SFX/music bed) |

Get this right on music first (the format that already exists) and
podcast/audiobook inherit a clean pattern instead of copying today's warts.

## Ties to work already in flight

- **vox-us4g** (CLI has no first-class playlist) is subsumed: the Program model
  is the "explicit list", and consume-only CLI verbs are the playlist half of it.
- **vox-ig52** (client-observable failure) generalizes: a podcast episode or
  chapter that fails to generate must surface via `status` (`part.status =
  failed` + reason), never vanish. The observability contract is the same.
- **h7h5** (LLM-authored music prompts) is the template: the LLM authors, vox
  pipes to ElevenLabs, the daemon plays. Podcast scripts and audiobook chapters
  are the same pattern with a different engine.

## Open design decisions (for the design session)

1. **Entry points.** One `program` tool/verb, or ergonomic per-format commands
   (`/music`, `/podcast`, `/audiobook`) that all produce Programs? (Lean:
   per-format commands, one shared Program domain model underneath.)
2. **Simultaneous playback.** Can a spoken Program run in the foreground while a
   music Program plays a ducked bed (audiobook-with-score)? Compelling, but adds
   a mixing concern.
3. **Persistence & manifest.** Program = a directory with a manifest (parts +
   metadata + casting). Define the minimal manifest the CLI needs to play/advance
   without the daemon regenerating.
4. **Length & credit budgeting.** Podcasts/audiobooks are long (many paid
   minutes). Need length caps + a cost confirmation before generation; playback
   stays free.
5. **Cast management.** How speaker/character → `voice_id` assignment works; use
   IVC/designed voices for v3 (PVC not yet optimized for v3).
6. **Engine choice per format.** Direct-to-API (compose / text-to-dialogue / TTS)
   and own the Program model in the daemon — lighter and a better daemon fit than
   driving ElevenLabs Studio "Projects" as the backend. Confirm.
7. **Formal model.** The Program lifecycle (author → generating → ready → playing
   → advancing/rotating → failed, per format) is a state machine — z-spec it
   before implementation, same trigger as vox-ig52.

## Decisions (operator ruling, 2026-07-05)

1. **Per-format commands, per-format LLM instructions.** `/music`, `/podcast`,
   `/audiobook` each carry their *own* authoring instructions to the LLM (as
   `commands/music.md` does today), independently fine-tunable. One shared
   Program model underneath.
2. **No cross-program mixing.** Background effects/music are baked *into* the
   audiobook (or podcast) Program at generation time — one Program, bed embedded
   — not a separate music Program ducked underneath. This drops the
   simultaneous-playback/mixing concern entirely. Simpler.
3. **CLI addresses Programs *and* Parts.** It authors via structured input
   (§"The author/consume seam"), and it can `list` a program, `play` a program,
   and select a specific part in series — e.g. `vox music playlist playlist:2`
   (part 2). A Part = track / chapter / episode by index within the Program.
4. **Length/credit budgeting** — deferred; not now.
5. **Cast management** — deferred; not now.
6. **Direct-to-API** (not ElevenLabs Studio "Projects" as backend). Keep podcasts
   and audiobooks deliberately on the *simpler* side.
7. **Model first (z-spec), and fix lengths:**
   - **Music**: vary track length realistically + slightly randomized. Today
     every track is exactly 2m (`music_length_ms = 120000`) — unnatural. Should
     span a realistic range. (Near-term quick win, independent of the epic.)
   - **Podcast**: ~5–10 min per episode.
   - **Audiobook**: ~5 min chapters; a "book" up to ~30 min total (≈6 chapters).

These supersede the corresponding open questions above.

## Decisions (operator ruling, 2026-07-25 — structured-input spine + tidy music)

Phase 1 (music → Program generalization) is **built**: the music internals were
forward-integrated into a format-agnostic `voxd/programs/` package (the old
`voxd/music/` package is gone). The core state machine — `Program`,
`ProgramState` (16 invariants), `Part`, the `Format` enum with all three values,
the stores, `Filler`, and the `Producer`/`PlaybackPolicy` seams — already hosts a
second format without a rewrite. The remaining work is at the edges, and these
rulings scope it:

1. **One input type per format — approved.** Each format has a single input type
   for its authored content, and the CLI and MCP tool both build it. The
   "CLI = consume-only" framing is struck.
2. **Finite formats terminate; music loops.** A podcast or audiobook **ends** at
   the last part (a `Complete` / terminal transition); a playlist **loops**
   (rotate). *Interim latitude:* if the terminal transition is meaningfully
   harder to build, a podcast/audiobook may **loop for now** — ship looping and
   add termination later. The Z model still models the terminate target.
3. **Long-generation per-Part status — approved.** Promote `pending`/`generating`
   from the single `filling` flag to **stored per-Part state** so a minutes-long
   spoken generation is observable per part. This is the one state-signature
   change; model it in `audio-programs.tex` at the Phase-2 (podcast) design gate,
   before implementation.

**Order of work:** update the design docs (this pass), then **tidy music first**
onto that one input type (the reference), then podcast (Phase 2), then audiobook
(Phase 3).

### Why not ElevenLabs Studio as the backend (decided 2026-07-05)

Studio (formerly "Projects") is ElevenLabs' timeline **editor + stateful project**
workflow for long-form audio (chapters, paragraph-level generation, multi-voice
casting, in-timeline music/SFX beds, pronunciation dictionaries, selective
regeneration, publish/distribute). It is built for **humans producing a polished,
distributable book in an editor**. Our Programs are **agent-authored and played
through voxd** (and saved on our own disk) — a different shape. Decision: go
**direct-to-API** (Music / Text-to-Dialogue / TTS) and own the Program model.

- Direct-to-API keeps podcast/audiobook on the same rails as the playlist (Phase
  1): our store, our daemon, "generate a part → play it." A podcast episode is one
  Text-to-Dialogue call; an audiobook chapter is chunked TTS.
- Studio's real strengths (editor, cheap selective regen, publishing, distribution)
  are mostly irrelevant to "generate a 10-min program and play it," and its API has
  real limits (thin endpoint docs, **SFX not supported when streaming via the
  Studio API**, plan-gated quality). The two things worth borrowing — chapter
  structure and free playback — the Program model already gives us.
- **Studio stays in the back pocket** for a *future, different* capability: "export
  this program as a real, distributable, human-editable audiobook." That is a
  publish/export path, not the core Programs feature.

## Phasing (operator, 2026-07-05 — Phase 1 first)

**Phase 1 — move today's music onto the Program model + unlock playlist replay
(CLI + MCP). — BUILT** (music internals forward-integrated into `voxd/programs/`;
the `voxd/music/` package is gone). No new ElevenLabs engines; refactor + one new
capability.

- Generalize the existing music internals into the shared model, **`playlist`
  format only**: `TrackStore → PartStore`, `Playlist → Program`,
  `PoolFiller → Producer` (music engine). A Program becomes a first-class, named,
  **persisted** entity (directory + manifest) — not a filename pattern.
  **Subsumes vox-us4g.**
- Keep the existing MCP `music` authoring path (the LLM authors prompts) — it now
  *produces a Program* instead of loose files.
- **New capability:** CLI **and** MCP can `list` programs, `play` a program,
  `loop`/rotate it, and select a specific part (`vox music playlist playlist:2`)
  — consume-only on the CLI (free playback, no LLM).
- This proves the Program model, the manifest, the CLI part-addressing, and the
  status-observability contract on the format that already exists — de-risking
  Phases 2–3.
- **Fold in / coordinate:** vox-y3om (varied music length — we're in the
  generation path anyway) and vox-ig52's observability contract (`part.status`
  surfaced via `status`), since both refactor the same music path. Sequence so
  they don't collide.

**Phase 1.5 — tidy music (the reference for podcast and audiobook).**
Phase 1's core shipped, but the music command surface is inconsistent. Fix it on
music first so podcast and audiobook copy a clean pattern. The problems: the CLI
has **no `on` authoring verb** (only the MCP tool can author a pool); `new` sends
a **bare string** instead of building a `PromptSet`; the MCP surface mixes one
`music` tool (on/off via a `mode` argument) with separate `music_play`,
`music_new`, `music_next` tools; and `vox music list --json` is rejected
(vox-cnak). Decisions (2026-07-25):

- **One `PromptSet` object, built by both the CLI and the MCP tool**, then sent
  to the daemon. `new` builds a `PromptSet` too, not a bare string.
- **MCP tools mirror the CLI structure.** One tool per command group, named for
  the group (`music`), with the subcommand (`on`/`off`/`play`/`next`/`new`/
  `list`/`get`/`remove`) as an argument — not one tool per subcommand. The same
  mapping applies to every group, so `rec` (and later `podcast`/`audiobook`)
  folds to the same shape in this pass or immediately after.
- **The CLI reads authored input from stdin** (Unix pipe): `cat pool.json | vox
  music on`. No `--file` flag. `vox music on` is new — the CLI has no `on` verb
  today.
- **`--json` works in every position** on the music subcommands.

No state-machine change — command surface and wiring only.

**Phase 2 — Podcast.** Text-to-Dialogue engine, multi-speaker, 5–10m episodes;
`/podcast` with its own authoring instructions. Slots into the Program frame.

**Phase 3 — Audiobook.** TTS long-form, ~5m chapters / ~30m books (beds a
fast-follow); `/audiobook` with its own authoring instructions.

## Sources

- ElevenLabs Music API — compose, `music_length_ms` (3s–10min), `force_instrumental`,
  composition plans (free), `music_v2`.
- ElevenLabs Text to Dialogue (eleven v3) — multi-speaker turns, audio tags,
  ~3k char/render, not for real-time; the podcast/character engine.
- ElevenLabs Studio / Audiobooks — chapters, paragraph-level generation, multi-voice,
  music/SFX beds, **playback of generated audio is free**, selective regeneration.
- ElevenLabs TTS long-form — `multilingual_v2` (quality), Flash/Turbo (up to 40k chars).
