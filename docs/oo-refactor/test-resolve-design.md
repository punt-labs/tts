# Design: test_resolve.py — resolve_voice_and_language coverage

Date: 2026-05-15
Status: REVISED — peer review by rej 2026-05-15, revisions applied
Bead: vox-hy07

## Problem

`resolve.py` has 50% test coverage. Lines 80–115 (`resolve_voice_and_language`)
are completely untested. The function contains conditional fallback logic with
6 distinct paths, any of which could silently break.

`split_leading_expressive_tags`, `strip_expressive_tags`, and `apply_vibe`
are partially covered by `test_server.py` — those are not the target of this
work.

## Target: resolve_voice_and_language

```python
def resolve_voice_and_language(
    provider: TTSProvider,
    voice: str | None,
    language: str | None,
    *,
    config_dir: Path | None = None,
) -> tuple[str, str | None]:
```

### Six paths to test

| # | Input | Expected behavior |
|---|-------|-------------------|
| P1 | explicit voice + explicit language | validate language, resolve_voice(voice, language), no language inference |
| P2 | explicit voice + no language | resolve_voice(voice), infer_language_from_voice(voice) |
| P3 | no voice + explicit language | get_default_voice(language), resolve_voice(voice, language) |
| P4 | no voice + no language | use provider.default_voice, resolve_voice(voice), infer_language_from_voice |
| P5 | voice from config + VoiceNotFoundError | log warning, fall back to provider.default_voice, continue |
| P6 | explicit voice + VoiceNotFoundError | re-raise, do not swallow |

### Additional edge cases

| # | Scenario |
|---|---------|
| E1 | language validated via validate_language() — invalid code raises ValueError |
| E2 | voice from config found, VoiceNotFoundError, fallback + language provided (line 109-110) |
| E3 | voice from config found, VoiceNotFoundError, fallback + no language (line 111-113) |

## Mock strategy

`TTSProvider` is a Protocol. Use `MagicMock(spec=TTSProvider)` with explicit
return values for `default_voice`, `get_default_voice()`,
`resolve_voice()`, `infer_language_from_voice()`.

**Critical: always set `mock.default_voice = "some-voice"` explicitly.** If not
set, `mock.default_voice` returns a `MagicMock` object (not a string), which
silently passes through the logger.info format and produces garbage output
without raising. Set it before calling the function.

**Critical: always set `mock.infer_language_from_voice.return_value = "en"`
explicitly** (or whatever language is expected). If not set, the mock returns a
MagicMock object, which may pass `== "en"` assertions accidentally. Configure
explicitly.

`_config.read_field()` must be mocked for ALL paths because line 84 checks
`if voice is None` — for explicit-voice paths (P1, P2, P6), voice is not None
so the block is never entered and the mock is not needed. For P3, P4, P5, E2,
E3, the block is entered and `_config.read_field` is called.

Patch target: `patch("punt_vox.resolve._config.read_field")` — not
`punt_vox.config.read_field`. The alias `_config` is bound at import time;
patching the source module attribute after the fact does not affect the
already-bound name in resolve.py.

- P4: mock returns `None` (no session voice in config)
- P5, E2, E3: mock returns `"session-voice"` (config voice exists but fails)

**For P5 (fallback succeeds after VoiceNotFoundError):** the fallback path calls
`resolve_voice` twice — once for the config voice (raises) and once for the
default voice (succeeds). Use list form of `side_effect`:

```python
mock.resolve_voice.side_effect = [VoiceNotFoundError("not found"), "Rachel"]
```

Mock consumes list side effects one at a time. Single-exception form would
raise on both calls, breaking the fallback.

**Do not use real config files or real providers.**

## File to create

`tests/test_resolve.py`

Structure:

```
class TestResolveVoiceAndLanguage      # the main target — all 9 cases below
    test_explicit_voice_and_language          # P1: both provided, resolve with both
    test_explicit_voice_infers_language       # P2: voice only, language inferred
    test_language_only_uses_provider_default  # P3: language only, voice from provider
    test_no_inputs_uses_provider_default      # P4: neither provided, all from provider
    test_config_voice_fallback_on_not_found   # P5: list side_effect [raise, succeed]
    test_explicit_voice_raises_on_not_found   # P6: explicit voice, VoiceNotFoundError
    test_invalid_language_raises              # E1: real invalid code, not mocked
    test_config_voice_fallback_with_language  # E2: fallback + language provided
    test_config_voice_fallback_no_language    # E3: fallback + no language (infer)
```

**No copying from test_server.py.** The `split_leading_expressive_tags`,
`strip_expressive_tags`, and `apply_vibe` functions are adequately covered
there. PL-PL-3 requires one test file per source module — `test_resolve.py`
satisfies that by existing, even if it focuses on the uncovered paths.
A comment at the top of the file directs readers to `test_server.py` for the
helper function tests.

## Coverage target

Lines 80–115 fully covered. Overall resolve.py from 50% → ≥95%.
