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

## Running Tests

```bash
# Full suite (required before every commit)
uv run pytest tests/ -v

# Single file
uv run pytest tests/test_hooks.py -v

# Single test
uv run pytest tests/test_hooks.py::TestHandleStop::test_voice_mode_blocks_clean_reason -v

# With coverage
uv run pytest tests/ --cov=src --cov-report=term-missing
```

## Integration Tests

Tests requiring real API credentials are marked `@pytest.mark.integration` and excluded from the default run. None currently exist — all provider tests use mocks. The marker is reserved for future smoke tests against live APIs.

## Quality Gates

Tests are one of the gates in `make check`, which must pass before every commit:

```bash
make check
```

All gates must show zero errors. No exceptions for "pre-existing failures."
