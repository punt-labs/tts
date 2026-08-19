# Vox

Part of [Punt Labs](https://github.com/punt-labs). This repo must be checked out inside the `punt-labs/` workspace meta-repo so that org-wide configuration loads via Claude Code's ancestor directory walk:

- **`punt-labs/CLAUDE.md`** — org workflow, delegation model, beads issue tracking, tool configuration
- **`punt-labs/.claude/rules/python-*.md`** — 19 Python OO coding rules, scoped via `paths:` frontmatter (load on-demand when `.py` files are touched)
- **`punt-labs/.envrc`** — git identity, beads DB connection, API keys from platform keychain
- **`punt-kit/standards/`** — canonical reference docs

If cloned outside the workspace, these rules and configuration will not be present.

**OO Python standards adopted 2026-05-13.** The codebase does not yet fully comply. Every commit must improve OO scores (`make check-oo`), never regress. Do not match existing code patterns that violate the rules — write new code to the standard and improve touched files incrementally.

Text-to-speech CLI, MCP server, and Claude Code plugin. Supports ElevenLabs (premium), AWS Polly, and OpenAI TTS.

- **Package**: `punt-vox`
- **CLI**: `vox`
- **MCP server**: `vox-server`
- **Python**: 3.13+, managed with `uv`

## Mandatory Reading

Source-of-truth documents, `@`-imported so they load into context at session
start. Read them before writing code.

@docs/WORKFLOW.md
@../punt-kit/standards/architecture.md
@../punt-kit/standards/oo.md
@../punt-kit/standards/python.md

`WORKFLOW.md` is the three-loop development process (backlog → PR → mission),
with pseudocode and an entry/exit Z schema at each level. The
`punt-kit/standards` imports are the org's canonical engine-and-clients
architecture, object-oriented stance, and Python standard (including the OO
ratchet vox already runs) — cross-repo (external) imports, so the first load
may ask for approval.

## Architecture

### How synthesis works

An agent (or human) calls an MCP tool or CLI command requesting speech. The request flows through the thin client layer (`client.py` → WebSocket → `voxd`). The daemon selects a provider (ElevenLabs > OpenAI > Polly > platform fallback), synthesizes audio, optionally caches the result (content-addressed by text+voice+provider via MD5), and plays it through the system audio stack (afplay on macOS, ffplay on Linux). Long texts are split by `core.py` into sentence-boundary chunks and synthesized in parallel.

### Key architectural boundary: daemon vs. clients

**`voxd`** is the persistent audio daemon — it owns the playback queue, provider connections, cache, and system service lifecycle. It has no MCP awareness, no session state, no project context. **Clients** (`VoxClient`/`VoxClientSync`) are lightweight WebSocket wrappers. The MCP server (`server.py`) and hook handlers (`hooks.py`) are both thin clients of `voxd` — they translate MCP/hook semantics into daemon RPC calls. No business logic in the client layer.

This separation means: daemon bugs are about audio, providers, caching, and system paths. Client bugs are about session state, MCP protocol, and hook classification. They never overlap.

### Module map

| Module | Responsibility |
|--------|---------------|
| `types.py` | Domain types: `TTSProvider` protocol, `AudioProviderId`, `AudioRequest`, `AudioResult`, `HealthCheck`, `MergeStrategy` |
| `core.py` | `TTSClient` — provider-agnostic orchestration: batching, pair stitching, audio merge, `split_text()` |
| `voxd.py` | Audio daemon. WebSocket server: synthesize, chime, record, voices, health. Playback queue, dedup, cache. System paths (Homebrew on macOS, FHS on Linux). |
| `client.py` | WebSocket client for `voxd`. `VoxClient` (async), `VoxClientSync` (sync). Lightweight — stdlib + websockets only. |
| `config.py` | Two config files: `vox.md` (durable prefs) + `vox.local.md` (ephemeral state). Routes by `DURABLE_KEYS`/`EPHEMERAL_KEYS` frozensets. |
| `hooks.py` | Hook handlers for Claude Code events — per-event dispatch (`handle_stop`, `handle_notification`, `handle_vibe_nudge`, `handle_pre_compact`, `handle_user_prompt_submit`, `handle_subagent_start`/`stop`, `handle_session_end`). Thin client of `voxd` via `VoxClientSync`. |
| `server.py` | FastMCP server (key: `mic`) — thin client of `voxd`. Session state in memory (`SessionState` dataclass). |
| `providers/` | Provider registry + ElevenLabs, OpenAI, Polly, macOS `say`, Linux `espeak-ng`. Each provider is the only file importing its SDK. |
| `cache.py` | MP3 cache for quip phrases. Content-addressed by (text, voice, provider) via MD5. Runs inside `voxd`. |
| `service.py` | System daemon lifecycle. macOS: `/Library/LaunchDaemons/`. Linux: `/etc/systemd/system/`. |
| `normalize.py` | Text normalization for speech: snake_case, camelCase, abbreviation expansion |
| `quips.py` | Centralized quip registry: all hook speech phrase pools as immutable tuples |
| `applet.py` | Lux display applet: builds element tree, connects to display server |

### Plugin structure

Everything the Claude Code plugin ships lives under `plugin/`, and nothing else does. The marketplace installs that one directory through Claude Code's `git-subdir` source (`"source": "git-subdir"`, `"path": "plugin"`), which is a blobless partial clone plus `git sparse-checkout set --cone plugin` — so an install never fetches `src/`, `tests/`, `docs/`, `scripts/`, `tools/`, `.github/`, or this repo's own `.punt-labs/` and `.claude/` working state. Cone mode always materializes the files sitting in the *repo root*, so the root documents (`README.md`, `DESIGN.md`, `CHANGELOG.md`, `prfaq.pdf`) still travel with an install; only whole directories are excluded.

Two rules follow, and both are load-bearing:

- **The surface must not reach outside itself at runtime.** A hook script may use `$HOME`, the `vox` binary on `PATH`, and paths under the *consumer's* repo root; it may not reference a file elsewhere in this repo, because that file does not exist on an installed plugin. `${CLAUDE_PLUGIN_ROOT}` is `plugin/`.
- **Paths inside the surface are relative to the surface.** `hooks.json` addresses its scripts as `${CLAUDE_PLUGIN_ROOT}/hooks/*.sh` and `session-start.sh` derives `PLUGIN_ROOT` from its own location, so both followed the move for free. The repo's *own* references — Makefile, CI, `scripts/`, tests, docs — did not, and are spelled `plugin/...` throughout.

| Path | Responsibility |
|------|---------------|
| `plugin/.claude-plugin/plugin.json` | Manifest. `mcpServers.mic` runs `vox mcp`, the binary on `PATH`, so no Python ships with the plugin. `"name": "vox-dev"` in the working tree; `scripts/release-plugin.sh` swaps it to `vox` for the tag. |
| `plugin/hooks/hooks.json` | Hook registration: SessionStart, PostToolUse (mic tools), Stop, PreCompact, Notification, UserPromptSubmit, SubagentStart/Stop, SessionEnd |
| `plugin/hooks/session-start.sh` | SessionStart: deploys commands, cleans retired commands, auto-allows MCP tools and skills, launches the session's Lux control panel |
| `plugin/hooks/suppress-output.sh` | PostToolUse (mic tools): formats MCP tool output for the UI panel |
| `plugin/hooks/notify.sh` | Stop → `vox hook stop` |
| `plugin/hooks/notify-permission.sh` | Notification (`permission_prompt`, `idle_prompt`) → `vox hook notification` |
| `plugin/hooks/pre-compact.sh` | PreCompact → `vox hook pre-compact` |
| `plugin/hooks/acknowledge.sh` | UserPromptSubmit → `vox hook user-prompt-submit` |
| `plugin/hooks/vibe-nudge.sh` | UserPromptSubmit → `vox hook vibe-nudge` |
| `plugin/hooks/subagent.sh` | SubagentStart/SubagentStop → `vox hook subagent-start` / `subagent-stop` |
| `plugin/hooks/farewell.sh` | SessionEnd → `vox hook session-end` |
| `plugin/commands/` | `/vox`, `/unmute`, `/mute`, `/recap`, `/vibe`, `/music`, `/model`, `/provider`, `/voice` |

Two gates hold the surface, both wired into `make lint` and the lint workflow:

- `scripts/check-skill-permissions.sh` — every `plugin/commands/*.md` has a matching `Skill()` grant in `plugin/hooks/session-start.sh`.
- `scripts/check-plugin-surface.sh` — every `${CLAUDE_PLUGIN_ROOT}`/`$PLUGIN_ROOT` reference anywhere in `plugin/` resolves to a path the surface actually ships, does not escape it via `..`, and is executable when it is a hook script. This is the gate for the reach-outside-itself rule above, which is otherwise a *silent* break: the path is present in the source tree and absent on every installed copy, so the hook runs, finds nothing, and the feature is quietly gone. It fails closed — if `hooks.json` stops carrying the placeholder at all, the gate errors rather than passing vacuously.

See `docs/architecture.tex` for the full system description.

## Code Quality

**OO ratchet:** `make check-oo` (part of `make check`) compares current OO scores against `.oo-baseline.json`. It passes only if no metric regressed on touched files and at least one metric improved. It fails if any metric got worse or nothing improved.

**Do not negotiate with the ratchet.** Do not edit `.oo-baseline.json` by hand except via `--rebaseline` for structural refactors. Do not suppress `check-oo`. If the ratchet fails, improve the code until it passes.

**"No metric improved" means do a good deed, not a rebaseline.** When `check-oo` reports *"no metric improved on any touched file,"* the ratchet is telling you this change grew or churned code without paying anything down. The correct response is a **genuine improvement** — extract a god-method, split an oversized module, collapse a conditional forest, in a touched file *or* unrelated nearby debt (e.g. the highest-complexity function in the tree; find it with `radon cc -s -n C -o SCORE src/`) — **never** a blanket `--rebaseline` to escape the gate. `--rebaseline` for a structural refactor is **not** a substitute for the paydown: even a large feature/refactor commit must leave at least one metric genuinely better. Distinguish a **scoped** rebaseline — only the specific `file+metric` entries that must grow to carry real feature substance, each with a one-line justification of *why* it's unavoidable, and every *improved* metric left at its old baseline so it registers as IMPROVED — from a **blanket** rebaseline that records all growth (improvements included) and retires no debt. The blanket form is the negotiation this section forbids. Also: when you rebaseline `.oo-baseline.json`, check `.oo-coupling-baseline.json` too — a structural change often grows coupling, and `check-coupling` (now a merge-base-scoped CI gate) fails if you skip it.

**The ratchet is tech-debt paydown — make medium-scale improvements, do not squeeze under the limit.** The ratchet exists to retire OO and complexity debt across the *whole* codebase a little at a time, the way you amortize a loan: every commit pays down some principal, no matter which file it touches. This is deliberately counterintuitive — it means taking on scope *beyond* the immediate task, and that added scope is the point, not a distraction from it. When you touch a file, make a *substantive* improvement to it — extract a class, break up a god method, replace a primitive-obsessed signature with a type, collapse a conditional forest — not the smallest metric nudge that scrapes past the "at least one metric improved" gate. Gaming the minimum is the failure mode: it burns more time (and review churn) than a real improvement, and it retires no debt. The waste to eliminate is relitigating tiny ratchet deltas and hunting for the cheapest legal change to pass; the goal is a genuine, medium-scale improvement at *every* opportunity. The test is simple: if the file you touched is meaningfully cleaner than you found it, the ratchet did its job — if you spent that time trying to change as little as possible, you used it wrong. This philosophy is org-wide; it belongs in the workspace ratchet policy (`../.claude/rules/python-oo-adoption.md`) as well. **Never game a size or complexity metric by stripping comments or docstrings** — `module_size` is retired by extracting classes and splitting modules, never by compressing prose. OO metrics are not about compressing comments.

**Org standards override review tools.** Copilot, Bugbot, and Cursor are advisory. When a review suggestion conflicts with rules in `../.claude/rules/python-*.md`, the rules win. Read the rules before accepting a reviewer's suggestion. PY-CC-1 (`__new__` as constructor) is the most common conflict.

**Verify outputs, not just metrics.** After writing a file, open it and read the content. `make check` passing does not mean the feature works — it means the code compiles and tests pass. Those are necessary but not sufficient.

Workflow:

1. Write code that improves OO quality on the files you touch.
2. `make check` runs `check-oo --check` automatically. If it fails, fix the regression.
3. After all checks pass, run `make update-oo` to write the new baseline.
4. Stage `.oo-baseline.json` and `.oo-audit.jsonl` with your commit — they are committed files.

Bootstrap (first time only): run `make update-oo` to create the initial baseline.

- `make check-oo` — OO ratchet against baseline.
- `make update-oo` — update baseline and append to audit log after improvements.
- `make report` — full diagnostics including per-file OO breakdown.
- `make metrics` — ABC complexity analysis.
- `make coverage` — test coverage HTML report.
- `make check-coupling` — coupling/cohesion analysis; in the `make check` chain and enforced in CI (merge-base scoped via `COUPLING_BASE`, fail-closed).
- `make check-suppressions` — lint/type suppression (`# noqa`, `# type: ignore`) count ratchet; in the `make check` chain and enforced in CI.

## Development Loop

The development workflow is **three nested loops** — the **backlog loop** (what to work on and in what order), the **PR loop** (one rollback-coherent merge), and the **mission loop** (one delegated piece of work) — defined authoritatively in **[`docs/WORKFLOW.md`](docs/WORKFLOW.md)**, `@`-imported above so it loads at session start. Read it before any code change; the pseudocode and the entry/exit Z schemas at each level are the source of truth for how work moves.

The vox-specific precision that used to live here now lives in `WORKFLOW.md`, intact: `make check` green before every commit; `make install` + `vox daemon restart` before exercising any MCP/hook/synthesis path, because the daemon serves old code until restarted; `make test` against the installed artifact; the two local review agents (`code-reviewer` and `silent-failure-hunter`) iterated to a zero-findings round; and the **audio demo confirmed by ear** — the canonical flight in [`docs/testing/manual-tests.md`](docs/testing/manual-tests.md), asking the operator after each audible step, because vox produces audio only a human can judge. The stateful-audio formal-modeling gate is [Formal Modeling (z-spec)](#formal-modeling-z-spec) below; the merge mechanics and recap-email discipline are in the org workflow (`../CLAUDE.md`).

### PR boundaries

Split by **rollback granularity**, not size. Ask: if this broke production, what reverts together? That is one PR. "The diff is large" and "separate concern" are prohibited split reasons — independent rollback capability and sequential dependency are the only valid ones. PRs should cover multiple steps — do not open a PR per step. Sequential steps in the same area belong in one PR.

**PRs do not need to be "pure," and purity is never a reason to hold back an improvement.** These PRs are agent-reviewed and squash-merged — the whole branch collapses to one commit on `main`, so the "normal fencing" (one-concern-per-PR, keep-the-diff-minimal, split-out-the-unrelated-bit) does not apply. Do not spend time policing scope: a docs tweak, an OO/complexity paydown, or an adjacent bug fix riding along with a feature PR is welcome, not a violation. **The operator explicitly rejects rules that make it harder to improve code.** If you are in a file and can make it better, do it — never revert or defer a genuine improvement to keep a PR "clean," and never open a separate PR solely for purity. The one real constraint is mechanical, not stylistic: when multiple agents share one worktree, don't let them edit the same uncommitted lines simultaneously — sequence them so no one's work is clobbered. That is about not losing work, not about scope.

**Known type checker workarounds:**

- **mypy vs pyright on boto3** (`providers/polly.py`): boto3-stubs types `boto3.client("polly")` correctly for mypy but pyright sees partially unknown overloads. Solution: `cast("PollyClientType", boto3.client("polly"))` with `# type: ignore[redundant-cast]` + `# pyright: ignore[reportUnknownMemberType]`.
- **pydub** has local type stubs in `typings/pydub/`. No `Any` annotations needed.
- **elevenlabs** ships `py.typed` but has pyright issues on some generated client code. Inline pyright ignores where needed.
- **`voxd` logs to stderr only in daemon mode.** Never use `print()` in server or daemon code.

## Testing

### Pyramid

| Layer | Make target | Runs in CI | What it covers |
|-------|-------------|------------|----------------|
| Unit | `make test` | yes | types, core, output, config, dirs, hooks, normalize, cache, keys, server, service, applet, all 5 providers |
| Integration | `@pytest.mark.integration` | no (needs AWS creds) | Real provider synthesis end-to-end |
| Shell scripts | `make lint` (shellcheck) | yes | plugin/hooks/*.sh, scripts/*.sh, install.sh |

Tests mirror source: one `test_*.py` per module plus `conftest.py` for shared fixtures. See [TESTING.md](TESTING.md) for the full testing philosophy and architecture.

### What good testing means in this project

Vox has five TTS providers, each with different SDKs, authentication, voice models, and error modes. The recurring failure mode is provider tests that pass with mocks but fail with real APIs because the mock doesn't match the SDK's actual behavior. Testing discipline:

- **Mock Polly responses need valid MP3 bytes.** pydub hands files to ffmpeg which rejects fake data. Use `AudioSegment.silent(duration=50)` in fixtures — never empty bytes or random data.
- **Use `side_effect=lambda` instead of `return_value`** for fresh mocks per call. `return_value` shares the same object across calls, causing aliasing bugs in tests that mutate results.
- **Every provider must test both success and auth failure paths.** A provider that can't authenticate should raise a clear error, not silently fall back.
- **Hook tests must verify per-event dispatch.** `hooks.py` routes each Claude Code event (stop, notification, vibe-nudge, subagent start/stop, session-end, …) to its handler, which emits the matching chime/speech. A wrong handler, a swallowed malformed payload, or a missed config-absent case means the wrong audio (or none) — test the per-event dispatch and the malformed-payload/config-absent paths explicitly.

## Formal Modeling (z-spec)

**When a change is a state machine, model it formally before implementing it.** A Z specification (`/z-spec:code2model`, fuzz type-checked) is REQUIRED — during the design phase, before the implementation mission dispatches — for the class of work below. This is not optional documentation: the design and its tests must satisfy the model, and every finding the formalization surfaces is resolved in the design review.

**Trigger — a change qualifies when it is a stateful subsystem AND any of:**

- It has 3+ modes/states with transitions between them (e.g. the music playlist `off → generating-first → playing-filling → rotating`; the vibe/signal state; the daemon playback lifecycle).
- Invariants must hold across transitions (e.g. `pool ≤ 12`, "at most one fill active", "playing ∈ pool", "generation only below full").
- A wrong transition corrupts state silently, crashes, or yields a UX expensive to discover late. bas7 (#291) shipped a broken loop precisely because a transition — advance-on-track-end — was never modeled and never listened to.

**Does NOT qualify** (skip the ceremony): pure I/O helpers, provider SDK wrappers, text formatting/normalization, single-function bug fixes with no state.

**What the model must contain:**

- A state schema with the invariants in its predicate (not scattered in prose).
- One operation schema per transition, with preconditions, postconditions, and framing.
- `fuzz -t` exits 0. For higher-stakes invariants, model-check with `/z-spec:test` (probcli) to explore the reachable state space, not just type-check.

**How it plugs into the workflow:**

- The design mission (or the leader) produces `docs/<feature>.tex` and commits it with the design artifacts.
- The leader's design review cites the model's findings and confirms the write-set and test plan satisfy it. Each finding is either designed-for or escalated to the operator — before implementation dispatches.
- The implementation's tests assert the modeled properties by name (e.g. "no immediate repeat", "fill stops at 12", "skip in the empty-pool state is a no-op").

**Precedent:** `docs/vox-notify.tex` (notification system) and `docs/audio-programs.tex` (vox-oayr, which generalises the earlier vox-1rxb four-state playlist model). It caught a crash path at design time — `/music next` before track #1 exists would rotate/pick on an empty pool and raise (`Rotate` requires `#pool ≥ 1`) — that the informal design missed. That is the whole point: design-time resolution is cheap; implementation-time discovery is a full defect cycle.

## Ethos & Delegation

Identity: `agent: claude` per `.punt-labs/ethos.yaml`. All code delegation uses ethos missions. Every non-trivial delegation has two phases: (1) **design mission** — describes problem, constraints, and invariants but does NOT prescribe a write set; (2) **implementation mission** — uses the write set produced by the design phase. The design mission's output IS the write set — the specialist decides what to create, split, or extract. This is critical: prescribing a write set before design prevents refactoring and forces code into existing modules.

**Project-local ethos (2026-07-03).** `.punt-labs/ethos/` is a vendored project-local directory, no longer a `punt-labs/team` git submodule. It is trimmed to the 15 identities vox delegates to (`claude`, `jfreeman`, `rmh`, `gvr`, `bwk`, `kpz`, `mdm`, `rop`, `adb`, `djb`, `edt`, `dna`, `mcg`, `rej`, `jms`) plus the engineering team and the durable mission records (`missions/` + `missions.jsonl`; verbatim per-session activity logs are gitignored, not tracked). Rationale: vox should be self-standing — it owns its identity data rather than depending on a shared submodule whose global setup is transitional. **Caveat:** ethos still *resolves* identities from the global `~/.punt-labs/ethos/` registry, not this vendored copy, so the trim does not yet reduce context injection and the vendored set is not yet proven complete (`claudia`, used for prose curation, exists only in global). Repo-primary resolution and completeness are a pending ethos-side change, coordinated with the ethos agent.

**No migration, backwards-compatibility, shim, or version-compat code — ever.** Punt Labs products have no installed user base to migrate, so every migration bridge, legacy-format/dir detector, `*-migrate` command, startup "run X to migrate" hint, backward-compat wrapper, and version shim is massive work product and complexity for zero reason. When a feature supersedes an old behavior or layout, DELETE the old path outright in the same change (forward integration, PY-RF-6) — never write a bridge to it. This is a **hard design-review gate**: the leader MUST strike any migration/compat/shim element from a design BEFORE dispatching implementation — it is a defect, not a feature. Do not let it reach implementation, where it costs a full build/test/review cycle before it gets ripped out (as Audio Programs Phase 1's Sec-3 migration mapping did, 2026-07-07). Keep only true false-positives: voice names ("shimmer"), live voice/language *compatibility validation*, comments stating "no legacy fallback".

**The COO must not read implementation files before writing the design spec.** "Add a handler to `hooks.py` at line 420" is a predetermined write set that prevents the specialist from making design decisions. "Extract the vibe signal accumulation into its own domain — the codebase has a config layer and a hooks layer, the implementation must follow code quality standards" gives the specialist latitude to decompose and restructure. This is how `hooks.py` grew past 1,000 lines pre-extraction — write sets were predetermined to existing files instead of letting the specialist extract new modules.

**One mission = one task.** Never give an agent multiple steps in a single prompt. Agent timeouts on multi-step prompts leave partial work and cause manual cleanup. One focused task per delegation.

**The COO does not write code.** Every code change, no matter how small, is delegated. There is no threshold below which a change is "too small to delegate." The only files the COO edits directly: `CHANGELOG.md`, `CLAUDE.md`, `DESIGN.md`, `README.md`, design docs, and plan files.

### Why these pairings

Vox spans three domains: (1) **audio/synthesis** — provider SDKs, audio formats, playback pipeline, daemon lifecycle — `rmh` for core Python, `gvr` for provider implementations, `kpz` for audio/playback performance; (2) **system integration** — launchd/systemd service, platform paths, hook scripts — `adb` for infrastructure, `djb` for auth/secrets, `mdm`/`rop` for shell/CLI; (3) **UX** — voice curation, vibe system, Lux applet — `claudia` for prose, `edt`/`dna` for visual.

| Task type | Worker | Evaluator |
|-----------|--------|-----------|
| Python core (`core.py`, `client.py`, `config.py`, `resolve.py`) | `rmh` (Hettinger) | `gvr` (van Rossum) |
| Provider implementations (Polly, OpenAI, ElevenLabs, say, espeak) | `gvr` | `rmh` |
| `voxd` audio daemon (WebSocket, queue, cache, playback) | `rmh` | `bwk` (Pike) |
| MCP server (`server.py`) tool surface | `rmh` | `mdm` (Pike) |
| CLI (`__main__.py`) command authoring | `mdm` | `rmh` |
| Hook scripts (`plugin/hooks/*.sh`) — bash, signal classification | `mdm` | `rop` (McIlroy) |
| System daemon install (`service.py`, launchd/systemd) | `adb` (Lovelace) | `djb` (Bernstein) |
| Provider auth / API-key handling / secrets | `djb` | `rmh` |
| Audio playback / pydub / ffmpeg pipeline | `gvr` | `kpz` (Karpathy) |
| Cache design (`cache.py`) — content-addressing, dedup | `rmh` | `kpz` |
| Voice / vibe / quip prose curation | `claudia` (Massimo) | `mcg` (Cagan) |
| Lux applet (`applet.py`) — visual surface | `edt` (Tufte) | `dna` (Norman) |
| Release / plugin name swap / cross-repo propagation | `adb` | `mdm` |
| Test infrastructure / fixtures (esp. mock MP3 bytes) | `rmh` | `gvr` |

### Pipeline selection

Use `standard` pipeline for new features, provider additions, daemon changes, or anything touching the hook/signal classification system. Use `quick` for single-module bugfixes that don't cross the daemon/client boundary. Review-cycle fix rounds (Copilot/Bugbot findings) use bare `Agent()`, not missions.

### Lessons from vox-0qi (2026-04-11)

- Write-set admission is the highest-value feature. It guaranteed parallel phases had zero file conflicts.
- Workers don't always read the contract via `ethos mission show` or submit results via `ethos mission result` — enforcement is partial. Verify the result artifact exists before closing.
- **Context/prompt split**: contract `context` carries the *what* (goal, constraints). Agent `prompt` carries the *how* (invocation instructions). Don't duplicate between the two.
- All 7 vox-0qi missions closed round 1. The reflect→advance cycle is untested under pressure.

See ethos docs for mission contract schema, worker prompt template, and the full mission lifecycle.

## Release

Use `/punt:auto release [version=X.Y.Z]`. Vox is a CLI + Plugin Hybrid — releases publish to both PyPI (`punt-vox`) and the marketplace.

### Dev plugin testing

The plugin uses dev/prod namespace isolation. Working tree has `"name": "vox-dev"` in `plugin/.claude-plugin/plugin.json`.

```bash
uv tool install --force --editable .        # Editable install (vox binary = working tree)
claude --plugin-dir plugin                  # Load dev plugin as vox-dev alongside prod vox
```

The directory passed to `--plugin-dir` is `plugin`, not the repo root: it has to be the same directory the `git-subdir` marketplace source checks out, or `${CLAUDE_PLUGIN_ROOT}/hooks/*.sh` resolves to paths that do not exist and every hook silently fails.

Release scripts: `scripts/release-plugin.sh` (swap `vox-dev` → `vox`), `scripts/restore-dev-plugin.sh` (restore dev state after tag).

## Key Documents

- `DESIGN.md` — ADR log
- `TESTING.md` — testing philosophy, fixture patterns, provider-specific test strategies
- `docs/architecture.tex` → `docs/architecture.pdf` — system architecture
- `prfaq.tex` → `prfaq.pdf` — product direction
- `docs/vox-notify.tex` — Z specification for notification system
@.punt-labs/ethos/CLAUDE.md
@.punt-labs/vox/CLAUDE.md
