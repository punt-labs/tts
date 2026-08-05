# OO Execution Plan v3 — Revised

Date: 2026-05-14
Revised after peer review by rej.

## Domain Model

### Core Domain: Speech

**SynthesisSpec** — Parameter set for synthesis. Exists as a frozen
dataclass. Gains behavior: `to_wire_message()`, `to_audio_request()`,
`with_segment_overrides()`, `resolved()` factory method (absorbs
ProviderSelection concept).

**Segment** — A chunk of text with per-segment overrides. Frozen
dataclass: text, overrides (SynthesisSpec | None). No speculative
fields.

**Voice** — A named voice with provider affinity and blurb. Frozen
dataclass. VoiceResolver owns the session > language > provider cascade.

**Utterance** — Under investigation. Pending analysis of whether the
5 synthesis paths share a coherent concept or are better served by
SynthesisSpec + Segment.

### Core Domain: Mood/Context

**Vibe** — Session mood context. Split per review into:
- (a) Data carrier: mood, tags, mode. No absorbed behavior.
- (b) Tag resolution moves to SignalLog (not Vibe).
- (c) `apply_vibe_for_synthesis` stays on SynthesisPipeline (needs
  provider knowledge).

**Signal** — Classified event from tool output. Frozen dataclass:
signal_type, timestamp, source_text.

**SignalLog** — Accumulated signal history. Mutable collection with
append(), counts(), serialize(), deserialize(). Absorbs
resolve_tags_from_signals() as a method.

### Core Domain: Music

**MusicTrack** — Saved track. Frozen dataclass: name, path, size_bytes,
modified. `display_line()`, `from_dict()`.

### Infrastructure

**CacheKey** — Content-addressed identity: text, voice, provider, digest.

**PlaybackResult** — Playback outcome: path, rc, elapsed_s, stderr, ts.

**HookPayload** — Three typed variants: StopPayload, BashPayload,
NotificationPayload. Other handlers take no stdin payload.

**Notification** — Stop-hook speech act: reason, phrase, delivery method.

### Dropped per review

- **HealthStatus** — DaemonHealth already exists, consumers immediately
  serialize. No second consumer.
- **ProviderSelection** — Subset of SynthesisSpec fields. Expressed as
  `SynthesisSpec.resolved()` factory method.
- **ConfigField** — Two frozensets and one function is simpler than 9
  dataclass instances with validators.

## Execution Plan

11 domain classes + 6 cleanup steps = 17 steps.

### Phase 1: Value Objects (no dependencies between steps)

**Step 1: CacheKey** — `cache.py`
- Frozen dataclass: text, voice, provider, digest property
- Replace cache_key() function and 3-arg API
- Files: cache.py, voxd/synthesis.py
- Tests: test_cache.py additions

**Step 2: PlaybackResult** — `voxd/playback.py`
- Frozen dataclass: path, rc, elapsed_s, stderr, ts
- Replace dict construction in _record_result()
- Files: voxd/playback.py, voxd/health.py, voxd/daemon.py,
  voxd/speech_handlers.py
- Tests: test_voxd_playback.py additions

**Step 3: MusicTrack** — `voxd/music/track.py`
- Frozen dataclass: name, path, size_bytes, modified
- display_line(), from_dict()
- Replace duplicated int(str(x)) formatting
- Files: server.py, __main__.py, voxd/music/generator.py
- Tests: new test_music_track.py

**Step 4: HookPayload** — `hook_payload.py`
- Three variants: StopPayload, BashPayload, NotificationPayload
- parse() function at hook entry point
- Other handlers (pre_compact, session_end, etc.) unchanged
- Files: hooks.py
- Tests: new test_hook_payload.py

### Phase 2: Domain Objects

**Step 5: Signal + SignalLog** — `signal.py`
- Signal: frozen dataclass, to_token(), from_token(), classify()
- SignalLog: append(), counts(), last(n), serialize(), deserialize()
- SignalLog gains resolve_tags() method (absorbs resolve_tags_from_signals)
- Files: hooks.py, config.py, server.py
- Tests: new test_signal.py

**Step 6: Vibe** — `vibe.py` (depends on Step 5)
- Data carrier only: mood, tags, mode
- classify() returns bright/neutral/dark (absorbs mood.classify_mood)
- to_prompt_layer(style) for music prompt generation
- Does NOT absorb apply_vibe_for_synthesis (stays on pipeline)
- Does NOT absorb resolve_tags (stays on SignalLog)
- Files: server.py, hooks.py, config.py, watcher.py, __main__.py
- Delete mood.py after migration
- Tests: new test_vibe.py

**Step 7: Voice + VoiceResolver** — `voice.py`
- Voice: frozen dataclass, name, provider, blurb
- VoiceResolver: resolve() with session > language > provider cascade
- Does NOT absorb featured-voice selection from who() (presentation)
- Files: voices.py, resolve.py, server.py
- Tests: new test_voice.py

**Step 8: SynthesisSpec behavior** — `types_synthesis.py`
- to_wire_message(text, request_id) — absorbs dict-building in client.py
- to_audio_request(normalized_text) — absorbs _build_audio_request()
- with_segment_overrides(voice, language, vibe_tags) — absorbs
  per-segment logic in server.py
- resolved() factory method — replaces ProviderSelection concept
- Files: client.py, server.py, voxd/synthesis.py
- Tests: test_types_synthesis.py additions

**Step 9: Segment** — `segment.py`
- Frozen dataclass: text, overrides (SynthesisSpec | None)
- No speculative fields (no index, total, status)
- Created during segment iteration in server.py and __main__.py
- Files: server.py, __main__.py
- Tests: new test_segment.py

**Step 10: Notification** — `notification.py`
- Class: reason (stop classification), phrase, delivery (chime/speech)
- Absorbs stop-hook speech selection logic from handle_stop()
- Files: hooks.py
- Tests: test_hooks.py additions

**Step 11: Utterance** — `utterance.py` (depends on Steps 8, 9)
- The speech act: text bound to voice parameters.
- Class composing SynthesisSpec: text, spec, request_id
- normalized_text(provider, model) — absorbs apply_vibe_for_synthesis
  from SynthesisPipeline (it operates on text + vibe_tags, which is
  Utterance's data, not the pipeline's)
- to_wire() — absorbs dict-building in client.py synthesize/record
- to_audio_request() — absorbs _build_audio_request in synthesis.py
- Scope: the request only. Does NOT own output path, playback, or result.
- Client-side and daemon-side as two representations with WebSocket
  message as serialization between them.
- Three working examples: MCP handler(seg_text, seg_spec), CLI
  client.synthesize(seg_text, **kwargs), daemon
  pipeline.synthesize_to_file(text, spec)
- Files: types_synthesis.py or new utterance.py, client.py, server.py,
  __main__.py, voxd/synthesis.py, voxd/speech_handlers.py
- Tests: new test_utterance.py

### Phase 3: Complexity Reduction (blocked on Phase 2)

**Step 12: audio_migration.py CC reduction**
- Extract _classify_file(), _resolve_conflict() from scan()
- Target: max_complexity <= 10

**Step 13: server.py CC reduction**
- Extract error-handling from _process_segments()
- Target: max_complexity <= 10

**Step 14: __main__.py CC reduction**
- After domain objects exist, CLI commands delegate to them
- Extract Method on remaining complex functions
- Target: max_complexity <= 10

**Step 15: chunked synthesis helper**
- Shared chunking from elevenlabs.py and openai.py
- New: providers/chunking.py

**Step 16: delete music_handlers.py**
- Legacy file replaced by voxd/music/ package
- Update imports, daemon.py wiring

**Step 17: __all__ on every module**
- Add __all__ to all modules missing it
- Mechanical pass

## Dependency Graph

```
Phase 1 (Steps 1-4): independent, can run in parallel

Phase 2:
  Step 5 (Signal/SignalLog) — no deps
  Step 6 (Vibe) — depends on Step 5
  Step 7 (Voice) — no deps
  Step 8 (SynthesisSpec) — no deps
  Step 9 (Segment) — depends on Step 8
  Step 10 (Notification) — no deps
  Step 11 (Utterance) — depends on Steps 8, 9; pending investigation

Phase 3 (Steps 12-17): blocked on ALL of Phase 2
```

## Acceptance Criteria

- method_ratio >= 0.80
- encapsulation_ratio == 1.0
- max_complexity <= 10 in every file
- module_size <= 300 (except __main__.py per PY-OO-2)
- classes_per_module <= 3
- class_to_func_ratio >= 0.5
- init_violations == 0
- public_attr_violations == 0
- circular_imports == 0
- max_lcom <= 0.8
- Every module has __all__
- Every source module has a test file
- make check passes with zero suppressions
