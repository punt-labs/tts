# Phase 2 Step 5: Signal + SignalLog Design

Date: 2026-05-16
Status: REVISED — peer review by rej 2026-05-16
Bead: vox-dexv

## Problem

The vibe signal accumulation system represents session events as a
comma-separated string `"tests-pass@14:32,lint-fail@15:00"` that is:

1. **Built by hand** in `handle_post_bash` (line 337):
   `token = f"{signal}@{timestamp}"`

2. **Parsed by hand** in `resolve_tags_from_signals` (line 189):
   `parts = [s.split("@")[0] for s in signals.split(",") if s]`

3. **Pruned by hand** in `handle_post_bash` (lines 342-344):
   `parts = new_signals.split(","); parts[-MAX_VIBE_SIGNALS:]`

4. **Read** in `handle_stop` and `session_end_cmd` via `config.vibe_signals`

The serialization format (`signal_type@HH:MM`) is owned by no class. It is a
domain concept — a classified event with a timestamp — embedded in a string.

## Proposed Classes

### Signal — in `src/punt_vox/signal.py`

```python
@dataclass(frozen=True, slots=True)
class Signal:
    """A single classified hook event with timestamp."""

    signal_type: str      # e.g. "tests-pass", "lint-fail", "git-push-ok"
    timestamp: str        # HH:MM format, e.g. "14:32"

    def to_token(self) -> str:
        """Serialize to wire format: 'tests-pass@14:32'."""
        return f"{self.signal_type}@{self.timestamp}"

    @classmethod
    def from_token(cls, token: str) -> Signal:
        """Parse 'tests-pass@14:32' into a Signal."""
        if "@" not in token:
            return cls(signal_type=token.strip(), timestamp="")
        signal_type, _, timestamp = token.partition("@")
        return cls(signal_type=signal_type.strip(), timestamp=timestamp.strip())

    @classmethod
    def now(cls, signal_type: str) -> Signal:
        """Create a Signal with the current local time."""
        # datetime imported at module level in signal.py (not inline)
        return cls(signal_type=signal_type, timestamp=datetime.now().strftime("%H:%M"))
```

### SignalLog — in `src/punt_vox/signal.py`

```python
class SignalLog:
    """Mutable ordered collection of session signals.

    Stores at most MAX_ENTRIES signals (LRU — oldest dropped first).
    Serializes to/from the comma-separated wire format used in vox.local.md.
    """

    __slots__ = ("_signals", "_max_entries")

    _signals: list[Signal]
    _max_entries: int

    MAX_ENTRIES: ClassVar[int] = 20  # = MAX_VIBE_SIGNALS

    def __new__(cls, max_entries: int = 20) -> Self:
        self = super().__new__(cls)
        self._signals = []
        self._max_entries = max_entries
        return self

    def append(self, signal: Signal) -> None:
        """Add a signal, evicting the oldest if over capacity."""
        self._signals.append(signal)
        if len(self._signals) > self._max_entries:
            self._signals = self._signals[-self._max_entries:]

    def counts(self) -> dict[str, int]:
        """Return count of each signal_type across the log."""
        result: dict[str, int] = {}
        for s in self._signals:
            result[s.signal_type] = result.get(s.signal_type, 0) + 1
        return result

    def last(self, n: int) -> list[Signal]:
        """Return the most recent n signals."""
        return self._signals[-n:]

    def resolve_tags(self) -> str:
        """Map accumulated signals to ElevenLabs expressive tags.

        Absorbs resolve_tags_from_signals() from hooks.py. Deterministic
        mapping — no LLM needed.
        """
        if not self._signals:
            return "[calm]"
        counts = self.counts()
        last_few = [s.signal_type for s in self.last(3)]
        ended_with_fail = any(s.endswith("-fail") for s in last_few)
        ended_with_pass = any(s.endswith("-pass") for s in last_few)
        had_push = "git-push-ok" in counts
        had_pr = "pr-created" in counts
        had_fails = sum(c for k, c in counts.items() if k.endswith("-fail"))
        had_passes = sum(c for k, c in counts.items() if k.endswith("-pass"))
        if had_fails > 0 and ended_with_pass:
            return "[relieved]"
        if had_push or had_pr:
            return "[satisfied]" if had_fails == 0 else "[relieved] [satisfied]"
        if ended_with_fail and had_fails > had_passes:
            return "[frustrated] [sighs]"
        if had_passes > 3 and had_fails == 0:
            return "[excited]"
        if had_passes > 0:
            return "[calm]"
        return "[calm]"

    def serialize(self) -> str:
        """Serialize to wire format for storage in vox.local.md."""
        return ",".join(s.to_token() for s in self._signals)

    @classmethod
    def deserialize(cls, raw: str, max_entries: int = 20) -> SignalLog:
        """Parse comma-separated token string into a SignalLog."""
        log = cls(max_entries=max_entries)
        if not raw:
            return log
        for token in raw.split(","):
            token = token.strip()
            if token:
                log.append(Signal.from_token(token))  # use append() for MAX_ENTRIES eviction
        return log

    def __len__(self) -> int:
        return len(self._signals)
```

## Updated callers in hooks.py

### handle_post_bash

Before:
```python
token = f"{signal}@{timestamp}"
current = read_config(config_dir).vibe_signals or ""
new_signals = f"{current},{token}" if current else token
parts = new_signals.split(",")
if len(parts) > MAX_VIBE_SIGNALS:
    new_signals = ",".join(parts[-MAX_VIBE_SIGNALS:])
write_field("vibe_signals", new_signals, config_dir)
```

After:
```python
log = SignalLog.deserialize(read_config(config_dir).vibe_signals or "")
log.append(Signal.now(signal))
write_field("vibe_signals", log.serialize(), config_dir)
```

### handle_stop

Before:
```python
tags = resolve_tags_from_signals(config.vibe_signals)
```

After:
```python
log = SignalLog.deserialize(config.vibe_signals or "")
tags = log.resolve_tags()
```

`resolve_tags_from_signals` is deleted. All callers updated.

## Invariants

1. **Wire format unchanged.** `"tests-pass@14:32,lint-fail@15:00"` — existing
   `vox.local.md` files remain readable after the change.

2. **MAX_VIBE_SIGNALS = 20** becomes `SignalLog.MAX_ENTRIES = 20`. The
   constant is removed from hooks.py.

3. **`classify_signal` is unchanged.** It returns `str | None` and lives in
   hooks.py. It is not part of this extraction.

4. **`resolve_tags_from_signals` is deleted** in the same commit as
   `SignalLog.resolve_tags()` is added. The `__all__` in hooks.py is updated.
   Tests that call `resolve_tags_from_signals` call `SignalLog.resolve_tags()`.

## Files Changed

- `src/punt_vox/signal.py` (new): `Signal`, `SignalLog`
- `src/punt_vox/hooks.py`: update `handle_post_bash`, `handle_stop`; delete
  `resolve_tags_from_signals`, `MAX_VIBE_SIGNALS`; import from signal.py
- `tests/test_hooks.py`: update any tests referencing `resolve_tags_from_signals`
- `tests/test_signal.py` (new): tests for `Signal`, `SignalLog`

## Completion Test

```bash
grep -rn "resolve_tags_from_signals\|MAX_VIBE_SIGNALS" src/ tests/
# Must return zero hits (both absorbed into SignalLog)
make check
```
