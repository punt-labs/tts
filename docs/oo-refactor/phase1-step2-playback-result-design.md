# Phase 1 Step 2: PlaybackResult Design

Date: 2026-05-15
Status: DRAFT — pending peer review
Bead: vox-ou7o

## Problem

The playback result dict `{"file", "rc", "elapsed_s", "stderr", "ts"}` is
constructed in two places and consumed in a third:

- `playback.py:_record_result()` (line 449) — builds the dict
- `speech_handlers.py` (lines 203–211) — builds an identical dict
- `health.py:full_payload()` (line 106) — reads `self._playback.last_result`
  and puts it into the health JSON response

`PlaybackQueue._last_result` is typed `dict[str, object] | None`. The dict
shape is implicit — no enforcement that both builders produce the same keys.

This is the Replace Dict with Class trigger (PY-RF-3): a dict with known string
keys passed through multiple functions.

## Proposed Class

In `src/punt_vox/voxd/playback.py`:

```python
@dataclass(frozen=True, slots=True)
class PlaybackResult:
    """Outcome of a single audio playback."""

    path: Path
    rc: int
    elapsed_s: float
    stderr: str
    ts: float

    def to_health_dict(self) -> dict[str, object]:
        """Serialize for the health endpoint JSON payload."""
        return {
            "file": str(self.path),
            "rc": self.rc,
            "elapsed_s": self.elapsed_s,
            "stderr": self.stderr,
            "ts": self.ts,
        }
```

Note: field name is `path: Path` (not `file: str`) — better domain modeling.
`to_health_dict()` handles the serialization to the wire format, preserving
the existing `"file"` key in the JSON so health clients are not broken.

## Updated API

| Before | After |
|--------|-------|
| `PlaybackQueue._last_result: dict[str, object] \| None` | `PlaybackQueue._last_result: PlaybackResult \| None` |
| `PlaybackQueue.last_result` property returns `dict` | returns `PlaybackResult \| None` |
| `PlaybackQueue.set_last_result(value: dict \| None)` | `set_last_result(value: PlaybackResult \| None)` |
| `PlaybackQueue._record_result(...)` builds a dict | builds a `PlaybackResult` |
| `SynthesizeHandler._record_result(...)` builds a dict | builds a `PlaybackResult` |
| `DaemonHealth.full_payload()` puts `last_result` dict directly | calls `result.to_health_dict()` |

## Invariants

1. **Wire format unchanged**: `to_health_dict()` produces exactly the same
   keys (`file`, `rc`, `elapsed_s`, `stderr`, `ts`) as the old dict. Health
   clients are not affected.

2. **`elapsed_s` rounding**: `_record_result` currently rounds: `round(elapsed, 4)`.
   The `PlaybackResult` stores the rounded value — rounding happens at
   construction time in `_record_result`, not in `to_health_dict`.

3. **`ts` is `time.time()` at construction** — set by `_record_result` at call
   time, not a default field value.

4. **`slots=True`** — value object, fixed fields.

5. **`frozen=True`** — result is immutable once recorded.

## Files Changed

- `src/punt_vox/voxd/playback.py`: add `PlaybackResult`, update `_record_result`,
  update `_last_result` type, update `last_result` / `set_last_result`
- `src/punt_vox/voxd/speech_handlers.py`: update `_record_result` to
  construct `PlaybackResult`
- `src/punt_vox/voxd/health.py`: update `full_payload()` to call
  `result.to_health_dict()` (or guard with `if result:`)
- `tests/test_voxd_playback.py`: update any tests that inspect `last_result`
  as a dict

## Completion Test

```bash
grep -rn '"file".*:.*str\|"rc"\|"elapsed_s"\|"stderr"\|"ts"' src/punt_vox/voxd/ | grep -v "to_health_dict\|#"
# Should return zero hits — all dict construction replaced by PlaybackResult
make check
```
