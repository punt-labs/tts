# Hidden Domain Classes Analysis

Date: 2026-05-14
Author: Ralph Johnson (rej)

## Summary

7 domain concepts are buried in procedural code as strings, dicts, and
scattered parameters instead of being modeled as classes.

| Rank | Hidden class | Files simplified | Key symptom |
|------|-------------|-----------------|-------------|
| 1 | **Vibe** | 8+ | 4 strings passed separately through every layer |
| 2 | **Signal / SignalLog** | 3 | comma-separated string parsed and counted by hand |
| 3 | **SynthesisSpec** (behavior gap) | 4 | 10-param signatures, duplicated wire-format building |
| 4 | **Voice** | 3 | tuple-keyed dict, resolution cascade in free functions |
| 5 | **MusicTrack** | 2 | copy-pasted dict formatting with int(str(x)) casts |
| 6 | **HookPayload** | 1 (6 functions) | data.get("key", default) + isinstance on every handler |
| 7 | **CacheKey** | 2 | (text, voice, provider) triple passed as 3 separate args |

## 1. Vibe — the session mood context

4 strings (mood, tags, signals, mode) passed separately through 8+
modules. Every architectural boundary reconstructs the concept from
its parts.

**State**: mood, tags, signals, mode
**Behavior**: classify(), resolve_tags(), append_signal(), to_prompt_layer(),
apply_to_text()
**Replaces**: entire mood.py, resolve_tags_from_signals() in hooks.py,
signal accumulation in handle_post_bash(), _layer_style_mood_feel() in
music.py, apply_vibe_for_synthesis() in synthesis.py, four-field dance
in SessionConfig

## 2. Signal / SignalLog — classified event from tool output

Three separate representations: bare string from classify_signal(),
SessionEvent dataclass in watcher.py, comma-separated string in config.

**State**: signal_type, timestamp, source_text
**SignalLog**: append(), counts(), last(n), serialize()/deserialize()
**Replaces**: comma-separated parsing in hooks.py, counting logic in
resolve_tags_from_signals()

## 3. SynthesisSpec behavior gap

The dataclass exists but doesn't know how to build its own wire message
or audio request. 10-parameter function signatures and duplicated
if-x-is-not-None blocks persist.

**New behavior**: to_wire_message(), to_audio_request(),
with_segment_overrides()
**Replaces**: 24 lines of duplicated dict-building in VoxClient,
_build_audio_request() (26 lines), 10-param _process_segments()
signature

## 4. Voice — named voice with provider affinity

VOICE_BLURBS is a dict[tuple[str, str], str]. Resolution cascade lives
in a free function. Featured voice selection is inline in server.py.

**State**: name, provider, blurb
**Behavior**: resolve(), blurb_text(), featured()
**Replaces**: tuple-keyed dict, filtering in who(), cascade in
resolve_voice_and_language()

## 5. MusicTrack — saved or in-flight track

Track metadata is dict[str, object] with int(str(raw_size)) casts
copy-pasted between server.py and __main__.py.

**State**: name, path, size_bytes, modified
**Behavior**: display_line(), from_dict()
**Replaces**: duplicated formatting in server.py and __main__.py

## 6. HookPayload — typed hook input

Every handler receives dict[str, object] and does data.get("key",
default) with isinstance guards. No schema, no validation.

**State**: union type (StopPayload, BashPayload, NotificationPayload)
**Behavior**: parse(raw) class method
**Replaces**: repetitive get+isinstance in 6 handler functions

## 7. CacheKey — content-addressed cache identity

(text, voice, provider) triple passed as three separate args to
cache_get and cache_put. The MD5 computation is a free function.

**State**: text, voice, provider, _digest
**Behavior**: filename(), path_in(cache_dir)
**Replaces**: three-argument repetition in cache API

## What does NOT need a class

- split_text(), normalize_for_speech(), stitch_audio() — pure functions
- VoxConfig — already a correct frozen dataclass snapshot
- SessionConfig — already a proper class with validated setters
- Quip phrase pools — immutable tuples, random.choice() is fine
