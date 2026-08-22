# Testing

No network or API dependencies. Every test runs fully offline — no API keys, no network, no audio hardware — but they do rely on local system binaries (ffmpeg for MP3 encoding, optionally espeak-ng).

## Philosophy

Vox has five TTS providers (ElevenLabs, OpenAI, Polly, macOS Say, espeak-ng), a plugin hook system, an MCP server, and a CLI. All of these talk to external services. The test suite proves correctness without ever calling them.

**Core principle**: mock at the provider boundary, test everything above it as real code.

## Architecture

Tests mirror source structure — one `test_*.py` per module — so the tree
below groups the suite by the area of the engine each file exercises. Files
prefixed `_` (`_cli_introspect.py`, `_program_fakes.py`) are shared helpers,
not test modules.

```text
tests/
  conftest.py                # Shared fixtures: mock clients, voice caches, valid MP3 bytes

  # Domain types, orchestration, text
  test_types.py              # Domain types re-exported from types.py
  test_types_synthesis.py    # SynthesisRequest / SynthesisResult / MergeStrategy
  test_types_health.py       # HealthCheck domain type
  test_core.py               # TTSClient orchestration: batching, SSML, stitching, merge
  test_synthesis_batch.py    # Parallel sentence-chunk batching
  test_chunked.py            # Chunked (long-text) synthesis splitting
  test_normalize.py          # Text normalization for speech
  test_resolve.py            # Provider/voice resolution
  test_voice_resolver.py     # Per-instance VoiceResolver cache load path
  test_provider_registry.py  # Provider registry selection order
  test_public_api.py         # __init__.py public library surface (__all__)

  # Output, playback, cache
  test_output.py             # Output path resolution
  test_output_formatter.py   # MCP tool-output formatting for the UI panel
  test_playback.py           # Audio playback (afplay/ffplay)
  test_cache.py              # Content-addressed MP3 quip cache

  # Config, state, paths, files
  test_config.py             # vox.md + vox.local.md durable/ephemeral routing
  test_config_isolation.py   # Config-dir isolation between repos/global
  test_frontmatter.py        # YAML frontmatter I/O: read/write/validate, OSError degradation
  test_markdown_doc.py       # Markdown-with-frontmatter document model
  test_private_state.py      # Ephemeral private-state file handling
  test_atomic_file.py        # Atomic file writes
  test_tool_owned_file.py    # Daemon-owned file ownership guard
  test_dirs.py               # Data/config directory resolution
  test_paths.py              # Path helpers (~/.punt-labs/vox, repo walk)
  test_path_status.py        # Path status reporting

  # Providers (mocked at the SDK boundary)
  test_polly_provider.py     # AWS Polly provider
  test_openai_provider.py    # OpenAI TTS provider
  test_elevenlabs_provider.py     # ElevenLabs TTS provider
  test_elevenlabs_music_provider.py  # ElevenLabs music provider
  test_say_provider.py       # macOS Say provider
  test_espeak_provider.py    # espeak-ng provider
  test_api_key_resolver.py   # API-key resolution across providers

  # WebSocket client for voxd
  test_client.py             # VoxClient / VoxClientSync
  test_client_gateway.py     # Client RPC gateway
  test_client_catalog_gateway.py  # Catalog (album) client gateway
  test_wire_reply.py         # Wire reply parsing

  # Claude Code hooks
  test_hooks.py              # Hook dispatchers: stop, vibe-nudge, notification
  test_hook_envelope.py      # Hook event envelope parsing
  test_hook_gate.py          # Hook enable/disable gating
  test_hook_payload.py       # Hook payload extraction
  test_hook_scripts.py       # plugin/hooks/*.sh shell entry points
  test_nudge_hook.py         # Vibe-nudge hook
  test_vibe_nudge.py         # Vibe-nudge cadence: threshold fire, reset, auto-only gating
  test_audible_notify.py     # Audible notification gating
  test_suppress_output.py    # PostToolUse output suppression
  test_control.py            # Control-action (zero-text) handling
  test_guidance.py           # Deposited guidance content
  test_claude_md.py          # CLAUDE.md @-import management

  # MCP server (mic)
  test_server.py             # Tools: unmute, vibe, who, notify, speak, status
  test_server_partition.py   # Z-spec partition tests for MCP state transitions
  test_server_music_tool.py  # music tool (subcommand dispatch)
  test_server_rec_music.py   # rec + music tool interaction
  test_server_enablement.py  # enablement tool (enable/disable)

  # CLI
  test_cli.py                # Typer CLI invocations via CliRunner
  test_cli_io.py             # CLI stdin/stdout plumbing
  test_cli_music.py          # vox music subcommands
  test_cli_rec.py            # vox rec subcommands
  test_cli_enablement.py     # vox enable / disable

  # Per-repo enablement & install
  test_enablement.py         # Enablement marker + import + settings
  test_settings_registration.py   # settings.json registration
  test_desktop_install.py    # Desktop (.mcpb) install
  test_install_script.py     # install.sh behavior

  # System service (launchd / systemd)
  test_service_health.py     # Service health check
  test_service_health_verify.py   # Health-verify probe
  test_service_installer.py  # Install/uninstall orchestration
  test_service_keys_env.py   # keys.env for the service
  test_service_launchctl.py  # macOS launchctl calls
  test_service_launchd.py    # launchd plist generation
  test_service_process.py    # Process lifecycle
  test_service_systemd.py    # systemd unit generation
  test_service_types.py      # Service domain types
  test_keys.py               # keys.env parsing and loading
  test_sibling_lock.py       # Sibling-process lock
  test_daemon_restarter.py   # Daemon restart logic
  test_doctor.py             # vox doctor diagnostics

  # voxd daemon internals
  test_voxd_daemon.py        # Daemon lifecycle
  test_voxd_router.py        # WebSocket request routing
  test_voxd_parse.py         # Wire request parsing
  test_voxd_wire_text.py     # Wire text handling
  test_voxd_wire_fault.py    # Wire fault handling
  test_voxd_synthesis.py     # Daemon-side synthesis
  test_voxd_speech_handlers.py    # Speech handlers
  test_voxd_play_handler.py  # Play handler
  test_voxd_playback.py      # Playback queue
  test_voxd_fetch_handler.py # Fetch handler
  test_voxd_chunked_fetch.py # Chunked fetch
  test_voxd_cache_handlers.py     # Cache handlers
  test_voxd_dedup.py         # Playback dedup
  test_voxd_chimes.py        # Chime handlers
  test_voxd_record_handler.py     # Record handler
  test_voxd_record_store.py  # Recording store
  test_voxd_rec_handlers.py  # rec subcommand handlers
  test_voxd_health.py        # Daemon health endpoint
  test_voxd_config.py        # Daemon config
  test_voxd_containment.py   # Path containment guard
  test_voxd_data_root_boundary.py # Data-root boundary guard
  test_voxd_system_handlers.py    # System (voices, etc.) handlers
  test_voxd_audit_log_level.py    # Audit log level
  test_voxd_log_level_handler.py  # Log-level handler

  # Logging
  test_logging_config.py     # Logging configuration
  test_append_log.py         # Append-only log
  test_log_append_handler.py # Append log handler
  test_log_sanitize.py       # Log sanitization (secret redaction)
  test_crash_logging.py      # Crash logging

  # Vibe, music phrases
  test_vibe.py               # Vibe state
  test_vibe_command.py       # Vibe command
  test_vibe_label.py         # Vibe label
  test_vibe_trace.py         # Vibe trace logging
  test_music_args.py         # Music argument parsing
  test_music_hint.py         # Music hint phrases
  test_music_phrases.py      # Music phrase pools
  test_music_prompts.py      # Music prompt building

  # OO ratchet & tooling
  test_oo_ratchet.py         # Ratchet gate against baseline
  test_oo_baseline.py        # Baseline read/write
  test_oo_score.py           # Score model
  test_oo_scorer.py          # Scorer
  test_oo_compare.py         # Baseline comparison
  test_oo_apply.py           # Baseline apply/update
  test_oo_audit.py           # Audit-log append
  test_oo_cli.py             # Ratchet CLI
  test_coupling_ratchet.py   # Coupling/cohesion ratchet
  test_suppression_ratchet.py     # Lint/type suppression ratchet

  music_player/              # Lux music-player surface (voxd/music_player/)
    conftest.py
    test_player.py, test_player_view.py, test_player_events.py
    test_scene.py, test_scene_mailbox.py, test_lux_scene_publisher.py
    test_lux_clients.py, test_lux_menu.py, test_lux_subscription.py
    test_lux_trace.py, test_composition.py, test_publish_button.py
    test_album_row.py, test_transport_row.py, test_playback_notice.py

  programs/                  # Music/background-audio programs (voxd/programs/)
    conftest.py
    test_program.py, test_state.py, test_mode.py, test_mode_transition_log.py
    test_loop.py, test_filler.py, test_fill_guard.py, test_fill_reconciler.py
    test_fill_recorder.py, test_fill_outcome.py, test_fill_signal.py
    test_producer.py, test_player.py, test_subprocess_player.py
    test_playback_policy.py, test_playback_source.py, test_playback_health.py
    test_rotate_policy.py, test_retry_machine.py, test_selection.py
    test_selection_playback.py, test_select_handler.py, test_select_signal.py
    test_catalog.py, test_library.py, test_library_handlers.py, test_store.py
    test_album_contents.py, test_album_id.py, test_album_tags.py
    test_manifest.py, test_part.py, test_part_tags.py, test_format.py
    test_control_channel.py, test_control_signal.py, test_change_signal.py
    test_switch_signal.py, test_interrupt_race.py, test_suspension.py
    test_active_context.py, test_status.py, test_wire.py, test_wiring.py
    test_handlers.py, test_service.py, test_sleeper.py, test_guard.py
    test_hex_token.py, test_identifiers.py, test_invariants.py
    test_properties.py
```

## Testing Pyramid

Vox has no NATS/relay/multi-user surface, so this is not biff's eight-tier
pyramid. Vox's actual shape is five TTS providers (mocked at the SDK
boundary), a `voxd` daemon over WebSocket, an MCP server, a CLI, Claude Code
hook shell scripts, a Lux display applet, and (arriving separately) a
`conversation_mode` subpackage that will spawn a real `claude -p --resume`
process and, eventually, drive a real Claude Agent SDK session. The tiers
below reflect that shape, not a copy of biff's.

```text
             ┌───────────┐
             │  Tier 4   │  sdk — real Claude Agent SDK session (reserved,
             │           │  no tests yet; lands with conversation_mode)
             ├───────────┤
          ┌──┤  Tier 3b  │  integration — real, credentialed provider API
          │  │           │  calls (reserved, no tests yet -- see below)
          │  ├───────────┤
       ┌──┤  │  Tier 3a  │  subprocess — real shell/git subprocess spawns
       │  │  │           │  (hook scripts, install.sh, plugin-surface gate)
       │  │  ├───────────┤
       │  │  │  slow     │  cross-cutting -- heavy real-subprocess/
       │  │  │           │  real-filesystem tests, wherever they occur
       │  │  ├───────────┤
       │  │  │  Tier 1   │  Unit -- mocked-provider-boundary, pure
       │  │  │           │  functions, in-memory state machines
       └──┴──┴───────────┘
```

`slow` is cross-cutting rather than a numbered tier because heavy tests do
not cluster in one place: today they are all real-git-subprocess ratchet
tests, but the marker exists for any test whose cost is disproportionate to
what it verifies, wherever it turns up next. `subprocess` is a numbered tier
because it is a specific, recurring shape in this codebase -- a test that can
only be honest by spawning a real process (`bash`, `sh`, `git`) because the
thing under test *is* a shell script or an install flow that cannot be
unit-tested in-process.

### Tier 1: Unit (default, unmarked)

No marker, no external service dependency. This is the vast majority of the
suite (~4,130 of ~4,340 tests) and runs on every `uv run pytest`, `make test`,
and `make check`. A handful of unmarked tests still spawn real local
subprocesses -- `test_core.py` and `test_public_api.py` call `ffmpeg` directly
outside the cached-MP3-bytes fixture path, and `test_service_process.py`
exercises the daemon-restart flow -- but none of them reach the network or a
credentialed API, so the tier's contract ("safe to run anywhere, no external
dependency") holds. Tests that spawn a real *shell script* under test move to
Tier 3a; local `ffmpeg`/interpreter spawns from tests whose subject is
in-process code stay here.

| What it covers | Representative files |
|-----------------|----------------------|
| Pure functions, domain types | `test_types*.py`, `test_normalize.py`, `test_resolve.py` |
| Mocked-provider-boundary synthesis | `test_polly_provider.py`, `test_openai_provider.py`, `test_elevenlabs_provider.py`, `test_say_provider.py`, `test_espeak_provider.py`, `test_core.py` |
| Real pydub/ffmpeg encode, mocked network | `test_chunked.py`, `test_core.py`, `test_public_api.py` (see [The MP3 Problem](#the-mp3-problem) -- within the mocked-provider fixture path the one real ffmpeg encode is cached once per session, not per test; tests that call `ffmpeg` directly, outside that fixture path, pay their own per-invocation cost) |
| WebSocket client/daemon wire protocol (in-process) | `test_client*.py`, `test_wire_reply.py`, `test_voxd_*.py` |
| Hook dispatch logic (config/audio patched) | `test_hooks.py`, `test_hook_envelope.py`, `test_hook_payload.py`, `test_nudge_hook.py` |
| Static shell-script assertions (no subprocess) | `test_hook_scripts.py` -- reads and greps the `.sh` files as text; does not execute them |
| Filesystem/config I/O against `tmp_path` | `test_config.py`, `test_frontmatter.py`, `test_atomic_file.py` |
| Service lifecycle, subprocess calls mocked via `@patch` | `test_service_process.py`, `test_service_launchctl.py`, `test_service_systemd.py` -- these spawn zero real processes; `subprocess.run` is patched at every call site |
| Z-spec partition tests | `test_server_partition.py` |
| Lux music-player / audio-programs subsystems (mocked Lux client) | `tests/music_player/`, `tests/programs/` -- ~854 tests, the largest subpackage, all in-memory/mocked |

### The `slow` marker: real-git-subprocess ratchet tests

`@pytest.mark.slow` is applied to four files whose tests each spin up a real
`git init`/`commit`/`checkout` sequence in a tmp directory and then invoke the
ratchet tool itself as a subprocess, per assertion:

| File | Tests | What it drives |
|------|-------|-----------------|
| `test_oo_ratchet.py` | 55 | `tools/oo_ratchet` against real tmp git repos |
| `test_oo_cli.py` | 10 | `tools/oo_ratchet.cli` argument parsing + a real git-repo fixture |
| `test_coupling_ratchet.py` | 46 | `tools/coupling` merge-base scoping against real tmp git repos |
| `test_suppression_ratchet.py` | 42 | `tools/suppression` counting + CLI dispatch against real tmp git repos |

These are the single largest identifiable per-test cost in the suite: on this
box, in isolation, the 153 tests took 32s (vs. ~0.02s/test for the unmarked
default tier) -- real `git` subprocess round trips dominate, not CPU work
(observed CPU utilization during these tests is well under 100% of one core,
meaning the wall time is mostly the OS scheduling and I/O of spawning `git`
processes, not computation). They are correct as real-subprocess tests --
the ratchet tools must be proven against real git history, not a mock -- so
the fix is not to make them lie about git; it is to make them opt-in.

### Tier 3a: `subprocess` -- real shell/git subprocess spawns

`@pytest.mark.subprocess` is applied to the files that can only be honest by
spawning a real process, because the thing under test is a shell script that
cannot run in-process:

| File | Tests | What it drives |
|------|-------|-----------------|
| `test_hook_gate.py` | 18 | Real `bash plugin/hooks/*.sh` invocations -- the per-repo enablement gate and the panel-spawn block |
| `test_plugin_surface.py` | 8 | Real `bash scripts/check-plugin-surface.sh` invocations against fixture surfaces |
| `test_install_script.py` | 13 | Real `sh install.sh` invocations under a sandboxed `PATH` of stub executables |
| `test_suppress_output.py` | 27 | Real `bash plugin/hooks/suppress-output.sh` invocations -- the PostToolUse two-channel display contract (also requires `jq` on `PATH`; auto-skipped when absent) |

`test_hook_scripts.py` looks like it belongs in this tier by name but does
not: it makes static text assertions over the `.sh` files (`grep`-shaped
checks for dead references), imports no `subprocess`, and spawns nothing. It
stays in the default tier.

This tier is the vox equivalent of biff's tier 3a (`StdioTransport`
subprocess tests) -- the same underlying problem (a real process, real
stdio/exit-code plumbing) applied to shell scripts instead of an MCP server
process. When `conversation_mode` lands and needs to spawn a real
`claude -p --resume` process, its subprocess-spawning tests belong under this
same `subprocess` marker, not a new one -- it is the same problem repeated in
a different domain.

### Tier 3b: `integration` -- reserved, deliberately empty

`@pytest.mark.integration` is declared but **no test currently carries it**.
This is deliberate, not an oversight, and it is a different situation from
biff's tier 3d/4: biff has one relay protocol to smoke-test against a real
deployment; vox has **five independently-billed provider APIs**
(ElevenLabs, OpenAI, Polly, macOS `say`, espeak-ng), each needing its own
credentials sourced from the platform keychain, never committed. Writing
real smoke tests against five live providers is a real piece of work --
credential plumbing, cost/quota management per provider, flakiness triage
for whichever provider is having a bad day -- and doing it well is out of
scope for a tiering/documentation pass with no operator-approved
credentials-in-CI plan. It is recommended as a follow-up (see below), not
done here by fabricating tests against APIs this pass has no way to verify.

### Tier 4: `sdk` -- reserved, deliberately empty

`@pytest.mark.sdk` is declared but **no test currently carries it**. This is
the vox equivalent of biff's tier 4: a marker reserved for a real Claude
Agent SDK session, real API cost per test, local-only by design and never in
CI. It has no tests yet because `conversation_mode` (the subsystem that will
need it) has not landed in this worktree -- the marker exists so that work,
when it arrives, has the tier ready rather than inventing its own name for
"the same problem biff already solved."

## Why the Suite Takes as Long as It Does

A `make check` pytest pass was observed taking 22+ minutes wall-clock during
today's review-fix cycle. Investigating that number directly:

**A clean, isolated run of the full unmarked suite (4,340 tests, before this
change) took 2:52 on this machine** (`uv run pytest`, no marker filter,
nothing else running). That is nowhere near 22 minutes. The single largest
identifiable per-test cost -- the git-subprocess ratchet tests, now marked
`slow` -- accounts for 32s of that 2:52; the shell-subprocess tests, now
marked `subprocess`, account for another 21s. Together they are ~18% of a
2:52 run, not the ~25-30% of a 22-minute run their per-test overhead would
suggest.

The gap between 2:52 (isolated) and 22+ minutes (observed) does not point to
a structural defect at the scale the observed number implies. The most
likely explanation is **CPU contention from concurrent agents sharing this
dev machine** -- this workspace's `CLAUDE.md` documents exactly this
scenario ("when multiple agents share one worktree..."), and the evidence is
consistent with it: an early measurement of just the ratchet-subprocess
files, taken while other activity may have been running, showed 25% CPU
utilization and took 4:35 for tests that took 32s under a later, uncontended
measurement of the identical files -- roughly an 8x slowdown with no code
change in between, which is a contention signature (I/O/scheduler wait), not
a CPU-bound one. **This is a real, if intermittent, cost**: whatever
consumed 8x the wall-clock time on a shared box will do it again to the next
person who runs `make check` while another agent is active, and it is worth
flagging plainly rather than resolving with only "the isolated number looks
fine."

**No `pytest-xdist` is installed.** `time`'s CPU-utilization column shows the
suite pinning close to one CPU-core's worth of work throughout even the
uncontended runs, on an 8-core machine. This is the highest-leverage
structural fix available and is *not* applied here (see Follow-ups) --
adding it and running `-n auto` would let the suite use the seven idle cores
instead of one, independent of whether the box is contended by other agents
or not.

Two candidates were investigated and ruled out:

- **Real ffmpeg encode per test** (the historical MP3 problem) -- checked:
  `conftest.py` generates the valid MP3 bytes exactly once per session
  (`_VALID_MP3_BYTES` module-level cache) and every mock provider reuses the
  cached bytes. Not a repeated cost.
- **The two `asyncio.sleep(5.0)` calls in `tests/music_player/`**
  (`test_composition.py`, `test_lux_scene_publisher.py`) look like an
  obvious 10-second tax at first read. They are not: both live inside a
  `_BlockingClient`/`_BlockingSceneAccessor` double used to prove a writer
  or publisher does not block on a slow render, and every test using them
  cancels the background task well before the 5s sleep resolves.
  `--durations=10` on both files confirms it: the slowest individual test
  call across both files is 0.24s. Left unmarked, correctly.

## The MP3 Problem

pydub hands audio files to ffmpeg, which validates MP3 headers. Mocks that return `b"fake mp3"` cause ffmpeg to reject the data, producing confusing failures far from the mock site.

Solution: `conftest.py` generates real MP3 bytes once via `AudioSegment.silent(duration=50)`, caches the result, and every mock provider response uses these bytes. This means stitching, merging, and file-write tests exercise real pydub/ffmpeg codepaths.

```python
# conftest.py — cached valid MP3 generation
def _generate_valid_mp3_bytes() -> bytes:
    silence = AudioSegment.silent(duration=50)
    buf = io.BytesIO()
    silence.export(buf, format="mp3")
    return buf.getvalue()
```

## Voice Cache Isolation

Provider voice caches are **per-instance**, not module-level. Each provider holds its own `self._voices = VoiceResolver(...)` (populated lazily on first use by calling the provider API); the OO refactor (Phase F, PR #264) moved these off the old module-level `VOICES` dict + `_voices_loaded` globals. Because the cache lives on the instance, constructing a fresh provider (or mock-backed provider) per test gives natural isolation — there is no module-level global to reset, and the old `autouse` `_populate_*_voice_cache` fixtures are gone.

Tests that verify cache-loading behavior itself (e.g., "does `resolve` call the API when the cache is empty?") construct a `VoiceResolver`/provider with an empty cache and assert the load path fires. (OpenAI keeps a static `VOICES` name→id constant map — that is a fixed lookup table, not a runtime API cache, so it needs no reset.)

## Mock Boundaries

Each provider fixture injects a mock client at construction time:

| Fixture | What's mocked | What's real |
|---------|--------------|-------------|
| `polly_provider` | `boto3.client("polly")` | `PollyProvider`, voice resolution, SSML generation |
| `openai_provider` | `openai.OpenAI()` | `OpenAIProvider`, chunking (4096 char limit), voice mapping |
| `elevenlabs_provider` | `elevenlabs.ElevenLabs()` | `ElevenLabsProvider`, voice resolution, streaming reassembly |
| `say_provider` | `platform.system()`, `shutil.which()` | `SayProvider`, voice resolution, command building |
| `espeak_provider` | `_find_espeak_binary()` | `EspeakProvider`, voice resolution, argument construction |
| `tts_client` | Only the provider (via `polly_provider`) | `TTSClient` orchestration: batching, stitching, merge strategies |

Mock responses use `side_effect=lambda` instead of `return_value` so each call gets a fresh response object. This prevents shared mutable state between assertions.

## Hook Testing

Hook tests (`test_hooks.py`) verify the Claude Code plugin integration — stop hooks, the vibe-nudge cadence, and notification dispatch. These mock at two boundaries:

1. **Config I/O** — `ConfigStore.write_field` / `write_fields` are patched to avoid filesystem interaction (and to inject `OSError` for the counter-persist-failure path)
2. **Audio dispatch** — `_chime_via_voxd` and `_speak_via_voxd` are patched to prevent actual playback

The `_make_config()` helper constructs `VoxConfig` objects directly, bypassing file parsing. This isolates hook logic from config parsing logic (which has its own tests in `test_config.py`).

Key patterns tested:

- Stop hook returns only a `♪` phrase with no internal data
- The vibe-nudge hook fires the reminder only on the Nth auto-mode prompt, then resets the counter
- Below the threshold, and in manual/off mode, the nudge emits nothing
- On a counter-persist failure the nudge stays silent (no reminder) rather than firing every prompt
- The nudge is non-blocking (never emits a `decision`) and synchronous

## Server Testing

Server tests (`test_server.py`) exercise the MCP tool functions directly —
`unmute()`, `vibe()`, `who()`, `notify()`, `speak()`, and `status()`. The
three subcommand-dispatched tools have their own files: `rec` (recordings) and
`music` (background program) in `test_server_rec_music.py` and
`test_server_music_tool.py`, and `enablement` (enable/disable) in
`test_server_enablement.py`. A `_patch_config` fixture creates a temp config
file and monkeypatches the module-level config path, so tools read/write real
YAML frontmatter in an isolated temp directory.

Provider construction is patched (`get_provider`, `TTSClient`) so no API clients are created. The tests verify argument threading (voice, language, rate, vibe tags), config side effects, and error messages.

## CLI Testing

CLI tests (`test_cli.py`) use Typer's `CliRunner` to invoke commands as subprocesses would. Provider construction and audio playback are patched. Tests verify exit codes, stdout/stderr output, and argument parsing.

## Config Testing

Config tests (`test_config.py`) use `tmp_path` fixtures to create the real
split config — `vox.md` (durable prefs) and `vox.local.md` (ephemeral state) —
inside a repo's `.punt-labs/vox/` directory, each with YAML frontmatter. Tests
verify:

- Durable/ephemeral routing: a field is written to `vox.md` or `vox.local.md`
  by its membership in `DURABLE_KEYS` / `EPHEMERAL_KEYS`
- Field reading with and without quotes
- Field writing (insert, update, multi-field atomic writes)
- Key validation (unknown keys raise `ValueError`)
- Edge cases: missing file, empty frontmatter, no frontmatter delimiters

## Markers

| Marker | Tier | What it requires | Default run? |
|--------|------|-------------------|---------------|
| *(none)* | 1 -- Unit | Nothing | Yes |
| `slow` | cross-cutting | Nothing (real `git`/subprocess round trips, no external service) | No |
| `subprocess` | 3a | `bash`/`sh`/`git` on `PATH` (present on every dev box and CI runner already) | No |
| `integration` | 3b | Real, credentialed provider API access (reserved -- no tests yet) | No |
| `sdk` | 4 | `ANTHROPIC_API_KEY`, real Claude Agent SDK session (reserved -- no tests yet) | No |

`pytest -m <marker>` always selects exactly that tier -- no marker is a
superset of another, matching the discipline biff's tier markers hold.

## Running Tests

```bash
# Default: tier 1 only -- fast, no external dependency, no real subprocess
uv run pytest

# Single file
uv run pytest tests/test_hooks.py -v

# Single test
uv run pytest tests/test_hooks.py::TestHandleStop::test_voice_mode_blocks_clean_reason -v

# slow -- real-git-subprocess ratchet tests
uv run pytest -m slow

# subprocess -- real shell-script/install.sh subprocess tests
uv run pytest -m subprocess

# Everything except the reserved, currently-empty integration/sdk tiers
uv run pytest -m "slow or subprocess or (not slow and not integration and not subprocess and not sdk)"

# With coverage
uv run pytest tests/ --cov=src --cov-report=term-missing
```

## Integration Tests

Tests requiring real, credentialed provider API calls are marked
`@pytest.mark.integration` and excluded from the default run. None currently
exist. See [Tier 3b](#tier-3b-integration----reserved-deliberately-empty)
above for why that is a deliberate call, not an oversight, and what would be
needed to populate it.

## Dev box vs CI

Every gap below has a concrete reason, not a permanent design choice unless
stated otherwise -- the same discipline biff's testing doc holds.

### On your dev box

| Tier | Command | When to run it |
|------|---------|-----------------|
| 1 (default) | `uv run pytest` | Constantly. No external dependency; this is the inner loop. |
| `slow` | `uv run pytest -m slow` | Before touching `tools/oo_ratchet`, `tools/coupling`, or `tools/suppression`. |
| `subprocess` (3a) | `uv run pytest -m subprocess` | Before touching `plugin/hooks/*.sh`, `install.sh`, or `scripts/check-plugin-surface.sh`. No extra infra -- `bash`/`sh`/`git` are already required to develop this repo at all. |
| `integration` (3b) | *(no tests yet)* | N/A until populated -- see the reserved-tier rationale above. |
| `sdk` (4) | *(no tests yet)* | N/A until `conversation_mode` lands and needs it. Local-only by design once it exists, same as biff's tier 4 -- real API cost per test, never in CI. |

### In CI

What runs on every push/PR (`.github/workflows/test.yml`): **the default
tier, plus `slow` and `subprocess`, as two separate `pytest` invocations in
the same job.** Before this taxonomy existed, all of these ran unmarked in
one `uv run pytest` call; splitting the invocation keeps that coverage
intact rather than silently dropping it the moment the markers exist and the
default `addopts` starts excluding them.

- **`integration`** -- no CI job, and none planned. Five independently
  billed provider APIs is a materially different problem from biff's one
  relay protocol; see the reserved-tier rationale above.
- **`sdk`** -- no CI job, by design, matching biff's tier 4. It will cost
  real money per test once it exists; it will never be a default CI job.

## Follow-ups Deliberately Left Out of This Pass

These are named here rather than done, because each is a bigger structural
change than "apply the taxonomy that already exists" -- reorganizing
directories, adding a new dev dependency, or standing up real
credentialed-API test infrastructure. Filed as recommendations, not silently
skipped:

- **Add `pytest-xdist` and run the default tier with `-n auto`.** The
  single highest-leverage speedup available: this box has 8 cores and the
  suite currently uses close to one throughout. This is a new dev dependency
  and needs its own verification pass (the `hermetic_config` /
  `hermetic_vibe_trace` / `hermetic_client_log` autouse fixtures in
  `conftest.py` use `tmp_path_factory`, which is `xdist`-safe by design, but
  that should be confirmed under `-n auto` before relying on it, not assumed).
- **Split `test_voxd_*.py` into a subpackage.** 21 files, the single largest
  flat cluster in `tests/` after `music_player/`/`programs/`. Not touched
  here per the task's explicit "no directory restructuring" constraint, but
  a real candidate for the same subpackage treatment `music_player/` and
  `programs/` already have.
- **Populate the `integration` tier.** Needs an operator decision on
  credentials-in-CI (or explicitly local-only, matching `sdk`) before any
  test is written against a live provider, plus a per-provider cost/quota
  plan for five separately billed APIs.
- **Wire the `subprocess`/`sdk` markers into `conversation_mode`'s tests
  when that subsystem lands.** The markers are reserved and documented now
  so that work does not reinvent them.
- **Investigate the machine-contention finding.** The 22-minute observation
  this task started from was not reproduced in isolation (2:52 for the full
  suite) but the 8x slowdown signature between a contended and an
  uncontended run of the identical ratchet-subprocess files is real and
  will recur for the next person running `make check` while another agent
  is active on a shared box. Worth a session-level fix (e.g. a `make check`
  guard that warns when other heavy processes are running) rather than a
  test-suite fix, since the suite itself is not the defect.

## Quality Gates

Tests are one of the gates in `make check`, which must pass before every commit:

```bash
make check
```

All gates must show zero errors. No exceptions for "pre-existing failures."
