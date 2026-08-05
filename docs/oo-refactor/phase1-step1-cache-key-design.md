# Phase 1 Step 1: CacheKey Design

Date: 2026-05-15
Status: GO — peer reviewed by rej 2026-05-15

## Problem

`cache.py` has 5 top-level functions that all take the same three parameters
— `(text, voice, provider)` — to identify a synthesis request. This triple
travels as three separate arguments through `cache_key()`, `cache_get()`, and
`cache_put()`. In `synthesis.py`, the same triple is constructed twice: once for
`cache_get` and once for `cache_put`.

This is the Push Down State smell (PY-RF-3): 3+ functions share the same
parameter tuple. That tuple is a domain concept — the content-addressed
identity of a cached audio file — and it has no class.

Current OO scores for `cache.py`:
- method_ratio: 0.00 (FAIL — target >= 0.80)
- class_to_func_ratio: 0.14 (FAIL — target >= 0.50)

## Proposed Class

```python
@dataclass(frozen=True, slots=True)
class CacheKey:
    """Content-addressed identity for a cached synthesis.

    The (text, voice, provider) triple uniquely identifies an anonymous
    synthesis request. The MD5 digest is byte-identical to pre-v4.2.1
    format so existing on-disk cache entries remain reachable after
    upgrade.
    """
    text: str
    voice: str | None
    provider: str | None

    @property
    def filename(self) -> str:
        """On-disk filename: 32-char MD5 hex + .mp3."""
        payload = f"{self.text}\0{self.voice or ''}\0{self.provider or ''}".encode()
        digest = hashlib.md5(payload, usedforsecurity=False).hexdigest()
        return f"{digest}.mp3"

    def path_in(self, cache_dir: Path) -> Path:
        """Absolute path for this key in the given cache directory."""
        return cache_dir / self.filename
```

## Updated API

The three-argument functions collapse to one-argument functions:

| Before | After |
|--------|-------|
| `cache_key(text, voice, provider) -> str` | Deleted. Use `CacheKey(...).filename`. |
| `cache_get(text, voice, provider) -> Path \| None` | `cache_get(key: CacheKey) -> Path \| None` |
| `cache_put(text, voice, provider, source) -> Path \| None` | `cache_put(key: CacheKey, source: Path) -> Path \| None` |
| `cache_clear() -> int` | Unchanged |
| `cache_status() -> CacheInfo` | Unchanged |

`_evict_if_needed()` is unchanged.

## Caller Update: synthesis.py

Before (two separate triples):
```python
cached = cache_get(normalized, resolved_voice, provider_name)
# ... later ...
cache_put(normalized, resolved_voice, provider_name, output_path)
```

After (one key, used twice):
```python
key = CacheKey(normalized, resolved_voice, provider_name)
cached = cache_get(key)
# ... later ...
cache_put(key, output_path)
```

## Invariants and Constraints

1. **Backward compatibility on disk**: The MD5 computation in `filename` is
   byte-identical to the old `cache_key()` function. Existing `.mp3` files in
   `~/.punt-labs/vox/cache/` remain reachable after the change. No migration
   needed.

2. **Anonymous cache only**: `CacheKey` is only used for anonymous (no api_key)
   synthesis paths. Per-call credential overrides in `synthesis.py` skip the
   cache entirely. `CacheKey` never touches credential material.

3. **Frozen + slots**: `@dataclass(frozen=True, slots=True)` per PY-CC-6.
   Value objects are immutable.

4. **No behavior in `_evict_if_needed`**: This private helper operates on the
   cache directory, not on a key. It remains a module-level function. Not
   every function in a module needs to become a method.

5. **`cache_clear` and `cache_status` are unchanged**: They operate on the
   cache directory globally, not on individual keys.

## What is NOT proposed

- Making `cache_get`/`cache_put` methods on `CacheKey`. The cache is a
  subsystem (`CACHE_DIR`, I/O, eviction) that is separate from the key's
  identity. Mixing I/O into the key class would violate SRP.

- A `Cache` class wrapping the directory. The v3 plan has this as a future
  step (Step 1 description references QuipCache). This step is only about
  `CacheKey` — one transformation at a time (PY-RF-1).

## OO Score Impact

| Metric | Before | After |
|--------|--------|-------|
| method_ratio | 0.00 | ~0.33 (2 methods + 5 top-level fns) |
| class_to_func_ratio | 0.14 | ~0.40 (2 classes, 5 fns) |

Still not at target (0.80 / 0.50) but directionally correct. The remaining
gap will be addressed when `cache_get`, `cache_put`, etc. become methods
on a `QuipCache` class in a future step.

## Files Changed

- `src/punt_vox/cache.py`: add `CacheKey`, update `cache_get`/`cache_put`
  signatures, delete `cache_key()` function; also add `slots=True` to
  existing `CacheInfo` (PY-CC-6 — same file touched, known violation)
- `src/punt_vox/voxd/synthesis.py`: construct `CacheKey` once, pass to both
  `cache_get` and `cache_put`
- `tests/test_cache.py`: update ALL tests — the `cache_key` import on line 14
  will fail to compile once the function is deleted. All tests using
  `cache_key()` must switch to `CacheKey(...).filename`.

## Peer Review Notes (rej, 2026-05-15)

- Real abstraction — not metric-driven. Three functions share the same triple.
- Class boundary is correct — `CacheKey` owns identity; I/O stays in module functions.
- MD5 byte-identity confirmed — `filename` property is byte-identical to old `cache_key()`.
- Scope is correct — defer `QuipCache` (directory operations) to future step.
- API change is unambiguous and clean.
- `slots=True` on `CacheInfo` is mechanical, must be done in same commit.
- All `cache_key()` test references must be updated (see test_cache.py:14).

## Completion Test (PY-RF-2)

After implementation:
```bash
grep -rn "cache_key(" src/ tests/  # must return zero hits
make check                          # must pass
```
