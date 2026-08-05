# Config Split Design: vox.md + vox.local.md

**Status:** SHIPPED (v4.7.5) — this is the settled, live layout (see DESIGN.md DES-036). The `.vox/config.md` path referenced below as "current" is the pre-split state and was removed; it is retained here as the original design record.

## 1. File Layout

```text
.punt-labs/vox/
  vox.md          # tracked in git — durable user preferences
  vox.local.md    # gitignored — ephemeral session state
  ephemeral/      # gitignored — ephemeral audio (unchanged)
```

Both files use the same YAML frontmatter format (`---` fences, `key: "value"` pairs) as the current `config.md`. No format change.

The legacy `.vox/` directory and `.vox/config.md` are removed entirely. The tracked `.vox/config.md` is deleted from git. `find_config()` drops the legacy path check.

## 2. Field-to-File Routing

| Field | File | Rationale |
|-------|------|-----------|
| `voice` | `vox.md` | User's preferred voice — persists across sessions |
| `provider` | `vox.md` | User's preferred TTS provider |
| `model` | `vox.md` | User's preferred TTS model |
| `notify` | `vox.md` | Notification mode default |
| `speak` | `vox.md` | Spoken notifications default |
| `vibe_mode` | `vox.md` | Vibe detection mode default |
| `vibe` | `vox.local.md` | Current mood string — session-scoped |
| `vibe_tags` | `vox.local.md` | Resolved expressive tags — session-scoped |
| `vibe_signals` | `vox.local.md` | Accumulated signal tokens — hottest field, cleared every session |

**Routing constant:**

```python
DURABLE_KEYS: frozenset[str] = frozenset({
    "model", "notify", "provider", "speak", "vibe_mode", "voice",
})

EPHEMERAL_KEYS: frozenset[str] = frozenset({
    "vibe", "vibe_tags", "vibe_signals",
})

ALLOWED_CONFIG_KEYS: frozenset[str] = DURABLE_KEYS | EPHEMERAL_KEYS
```

`write_field` and `write_fields` use field membership in `DURABLE_KEYS` vs `EPHEMERAL_KEYS` to pick the target file. No caller needs to know which file a field lives in.

## 3. Read Semantics

`read_config()` merges both files. Ephemeral wins on conflict (though conflicts should not arise given disjoint key sets — the merge order is defensive).

```python
def read_config(config_dir: Path | None = None) -> VoxConfig:
    """Read all config fields, merging vox.md and vox.local.md."""
    d = config_dir or DEFAULT_CONFIG_DIR
    fields: dict[str, str] = {}

    # Base layer: durable prefs
    durable = d / "vox.md"
    if durable.exists():
        fields.update(_parse_frontmatter(durable))

    # Overlay: ephemeral session state (wins on conflict)
    ephemeral = d / "vox.local.md"
    if ephemeral.exists():
        fields.update(_parse_frontmatter(ephemeral))

    return _fields_to_config(fields)
```

`read_field()` checks the correct file based on `EPHEMERAL_KEYS` membership. Ephemeral keys read from `vox.local.md`; everything else (durable and unknown) reads from `vox.md`.

```python
def read_field(field: str, config_dir: Path | None = None) -> str | None:
    d = config_dir or DEFAULT_CONFIG_DIR
    if field in EPHEMERAL_KEYS:
        return _read_single_field(d / "vox.local.md", field)
    return _read_single_field(d / "vox.md", field)
```

## 4. Write Semantics

`write_field()` and `write_fields()` route each key to the correct file. When `write_fields` receives a mix of durable and ephemeral keys, it batches writes per file (one read-write cycle per file, not per key).

```python
def write_field(key: str, value: str, config_dir: Path | None = None) -> None:
    d = config_dir or DEFAULT_CONFIG_DIR
    target = d / ("vox.md" if key in DURABLE_KEYS else "vox.local.md")
    _write_single(target, key, value)

def write_fields(updates: dict[str, str], config_dir: Path | None = None) -> None:
    for key in updates:
        if key not in ALLOWED_CONFIG_KEYS:
            allowed = ", ".join(sorted(ALLOWED_CONFIG_KEYS))
            msg = f"Unknown config key '{key}'. Allowed: {allowed}"
            raise ValueError(msg)
    d = config_dir or DEFAULT_CONFIG_DIR
    durable_updates = {k: v for k, v in updates.items() if k in DURABLE_KEYS}
    ephemeral_updates = {k: v for k, v in updates.items() if k in EPHEMERAL_KEYS}
    if durable_updates:
        _write_batch(d / "vox.md", durable_updates)
    if ephemeral_updates:
        _write_batch(d / "vox.local.md", ephemeral_updates)
```

## 5. Path Changes

### dirs.py

Replace the single-file default path with a directory path:

```python
# Before
DEFAULT_CONFIG_PATH = _REPO_SUBDIR / "config.md"

# After
DEFAULT_CONFIG_DIR = _REPO_SUBDIR   # Path(".punt-labs/vox")
```

`find_config()` changes from finding a single file to finding a directory that contains `vox.md` or `vox.local.md`:

```python
def find_config_dir(start: Path | None = None) -> Path | None:
    """Walk up from start (default: cwd) to find .punt-labs/vox/ dir.

    Returns the directory if either vox.md or vox.local.md exists inside it.
    No legacy fallback — .vox/ is removed.
    """
    path = (start or Path.cwd()).resolve()
    for parent in (path, *path.parents):
        d = parent / _REPO_SUBDIR
        if (d / "vox.md").exists() or (d / "vox.local.md").exists():
            return d
    return None
```

Drop `_LEGACY_REPO_DIR` and all `.vox/` references.

## 6. Caller-by-Caller Changes

### 6.1 config.py

**Signature changes — every function switches from `config_path: Path` to `config_dir: Path`:**

| Function | Current signature | New signature |
|----------|-------------------|---------------|
| `read_field` | `(field, config_path=None)` | `(field, config_dir=None)` |
| `read_config` | `(config_path=None)` | `(config_dir=None)` |
| `write_field` | `(key, value, config_path=None)` | `(key, value, config_dir=None)` |
| `write_fields` | `(updates, config_path=None)` | `(updates, config_dir=None)` |

This is a clean break — all callers are updated in the same PR.

**Internal implementation:**

- Extract `_parse_frontmatter(path) -> dict[str, str]` from the current `read_config` body.
- Extract `_write_single(path, key, value)` and `_write_batch(path, updates)` from the current `write_field`/`write_fields` bodies.
- Add `DURABLE_KEYS`, `EPHEMERAL_KEYS` constants.
- Remove `DEFAULT_CONFIG_PATH` re-export (replaced by `DEFAULT_CONFIG_DIR`).
- Remove `find_config` re-export (replaced by `find_config_dir`).

### 6.2 dirs.py

- Remove `_LEGACY_REPO_DIR = Path(".vox")`.
- Rename `DEFAULT_CONFIG_PATH` to `DEFAULT_CONFIG_DIR = _REPO_SUBDIR`.
- Rename `find_config()` to `find_config_dir()`, drop legacy `.vox/` fallback.
- `ephemeral_dir()` is unchanged (already uses `_REPO_SUBDIR`).

### 6.3 hooks.py

**Imports (line 34-39):**

```python
# Before
from punt_vox.config import VoxConfig, read_config, write_field, write_fields
from punt_vox.dirs import DEFAULT_CONFIG_PATH, find_config

# After
from punt_vox.config import VoxConfig, read_config, write_field, write_fields
from punt_vox.dirs import DEFAULT_CONFIG_DIR, find_config_dir
```

**handle_stop (line 268-269):**

```python
# Before
config_path = find_config() or DEFAULT_CONFIG_PATH
write_fields({"vibe_tags": tags, "vibe_signals": ""}, config_path)

# After
config_dir = find_config_dir() or DEFAULT_CONFIG_DIR
write_fields({"vibe_tags": tags, "vibe_signals": ""}, config_dir)
```

Both `vibe_tags` and `vibe_signals` are ephemeral keys — `write_fields` routes them to `vox.local.md` automatically.

**handle_post_bash (line 314, 348, 355):**

```python
# Before
def handle_post_bash(data: dict[str, object], config_path: Path) -> None:
    ...
    current = read_config(config_path).vibe_signals or ""
    ...
    write_field("vibe_signals", new_signals, config_path)

# After
def handle_post_bash(data: dict[str, object], config_dir: Path) -> None:
    ...
    current = read_config(config_dir).vibe_signals or ""
    ...
    write_field("vibe_signals", new_signals, config_dir)
```

`vibe_signals` is ephemeral — `write_field` routes to `vox.local.md`.

**handle_session_end (line 511, 527):**

```python
# Before
def handle_session_end(config: VoxConfig, config_path: Path) -> None:
    ...
    write_field("vibe_signals", "", config_path)

# After
def handle_session_end(config: VoxConfig, config_dir: Path) -> None:
    ...
    write_field("vibe_signals", "", config_dir)
```

**All CLI hook commands (stop_cmd, post_bash_cmd, notification_cmd, etc.):**
Pattern is identical across all — change `find_config()` to `find_config_dir()` and `DEFAULT_CONFIG_PATH` to `DEFAULT_CONFIG_DIR`:

```python
# Before (repeated ~8 times across lines 538-624)
config_path = find_config() or DEFAULT_CONFIG_PATH
if not config_path.exists():
    return
config = read_config(config_path)

# After
config_dir = find_config_dir() or DEFAULT_CONFIG_DIR
# No existence check needed — read_config handles missing files gracefully
config = read_config(config_dir)
```

Note: The current `if not config_path.exists(): return` guard is vestigial — `read_config` already returns safe defaults when the file is missing. With the split, this becomes even more natural since `read_config(config_dir)` handles zero, one, or both files missing. Remove the guard.

### 6.4 server.py

**_find_config (line 76-80):**

```python
# Before
def _find_config() -> Path | None:
    from punt_vox.dirs import find_config
    return find_config()

# After
def _find_config_dir() -> Path | None:
    from punt_vox.dirs import find_config_dir
    return find_config_dir()
```

**_seed_state_from_config (line 83-101):**

```python
# Before
def _seed_state_from_config(config_path: Path | None) -> SessionState:
    if config_path is None or not config_path.exists():
        return SessionState()
    from punt_vox.config import read_config
    cfg = read_config(config_path=config_path)
    ...

# After
def _seed_state_from_config(config_dir: Path | None) -> SessionState:
    if config_dir is None:
        return SessionState()
    from punt_vox.config import read_config
    cfg = read_config(config_dir=config_dir)
    ...
```

No file existence check needed — `read_config` handles missing files.

**vibe tool (line 472-474):**

```python
# Before
from punt_vox.config import write_fields
write_fields(updates, _find_config())

# After
from punt_vox.config import write_fields
write_fields(updates, _find_config_dir())
```

`vibe` tool writes a mix: `vibe_mode` goes to `vox.md`, `vibe`/`vibe_tags`/`vibe_signals` go to `vox.local.md`. `write_fields` handles the routing automatically.

**notify tool (line 749-751):**

```python
# Before
from punt_vox.config import write_fields
write_fields(updates, _find_config())

# After
from punt_vox.config import write_fields
write_fields(updates, _find_config_dir())
```

`notify` writes `notify` (durable), `speak` (durable), `voice` (durable) — all route to `vox.md`.

**speak tool (line 793-795):**

```python
# Before
from punt_vox.config import write_fields
write_fields(updates, _find_config())

# After
from punt_vox.config import write_fields
write_fields(updates, _find_config_dir())
```

`speak` writes `speak` (durable), `voice` (durable) — all route to `vox.md`.

**run_server (line 873-880):**

```python
# Before
config_path = _find_config()
_state = _seed_state_from_config(config_path)
if config_path is not None and config_path.exists():
    from punt_vox.config import read_field
    if read_field("speak", config_path) is not None:
        _speak_explicit = True

# After
config_dir = _find_config_dir()
_state = _seed_state_from_config(config_dir)
if config_dir is not None:
    from punt_vox.config import read_field
    if read_field("speak", config_dir) is not None:
        _speak_explicit = True
```

### 6.5 **main**.py

**Imports (line 27-31):**

```python
# Before
from punt_vox.config import read_config, write_field, write_fields
from punt_vox.dirs import DEFAULT_CONFIG_PATH, default_output_dir, find_config

# After
from punt_vox.config import read_config, write_field, write_fields
from punt_vox.dirs import DEFAULT_CONFIG_DIR, default_output_dir, find_config_dir
```

**vibe_cmd (lines 636-647):**

```python
# Before
cp = find_config() or DEFAULT_CONFIG_PATH
if mood == "auto":
    write_fields({"vibe_tags": "", "vibe": "", "vibe_mode": "auto"}, config_path=cp)

# After
cd = find_config_dir() or DEFAULT_CONFIG_DIR
if mood == "auto":
    write_fields({"vibe_tags": "", "vibe": "", "vibe_mode": "auto"}, config_dir=cd)
```

This writes a mix: `vibe_mode` (durable) goes to `vox.md`; `vibe_tags`, `vibe` (ephemeral) go to `vox.local.md`. `write_fields` routes automatically.

**notify_cmd (lines 671-678):**

```python
# Before
config_path = find_config() or DEFAULT_CONFIG_PATH
first_init = not config_path.exists()
updates: dict[str, str] = {"notify": mode}
if mode == "c" or (first_init and mode == "y"):
    updates["speak"] = "y"
...
write_fields(updates, config_path=config_path)

# After
config_dir = find_config_dir() or DEFAULT_CONFIG_DIR
first_init = read_field("notify", config_dir) is None
updates: dict[str, str] = {"notify": mode}
if mode == "c" or (first_init and mode == "y"):
    updates["speak"] = "y"
...
write_fields(updates, config_dir=config_dir)
```

The `first_init` check changes from file-existence to field-existence: if `notify` has never been written to `vox.md`, this is a first-time setup. The tracked `vox.md` ships with `notify: "n"`, so `first_init` is False for cloned repos — correct, since the default already has `speak: "y"`. All keys (`notify`, `speak`, `voice`) are durable — route to `vox.md`.

**speak_cmd (line 705):**

```python
# Before
write_field("speak", mode, config_path=find_config() or DEFAULT_CONFIG_PATH)

# After
write_field("speak", mode, config_dir=find_config_dir() or DEFAULT_CONFIG_DIR)
```

`speak` is durable — routes to `vox.md`.

**voice_cmd (line 720):**

```python
# Before
write_field("voice", name, config_path=find_config() or DEFAULT_CONFIG_PATH)

# After
write_field("voice", name, config_dir=find_config_dir() or DEFAULT_CONFIG_DIR)
```

`voice` is durable — routes to `vox.md`.

**status_cmd (line 743):**

```python
# Before
cfg = read_config(config_path=find_config() or DEFAULT_CONFIG_PATH)

# After
cfg = read_config(config_dir=find_config_dir() or DEFAULT_CONFIG_DIR)
```

**doctor (line 1038-1051):**
Remove the legacy `.vox/` directory check entirely. No migration, clean break.

### 6.6 resolve.py

**resolve_voice_and_language (line 85):**

```python
# Before
voice = _config.read_field("voice", config_path or _config.DEFAULT_CONFIG_PATH)

# After
voice = _config.read_field("voice", config_dir or _config.DEFAULT_CONFIG_DIR)
```

The function signature changes from `config_path: Path | None` to `config_dir: Path | None`.

**apply_vibe (lines 139-140):**

```python
# Before
tags = override_tags or _config.read_field(
    "vibe_tags", config_path or _config.DEFAULT_CONFIG_PATH
)

# After
tags = override_tags or _config.read_field(
    "vibe_tags", config_dir or _config.DEFAULT_CONFIG_DIR
)
```

The function signature changes from `config_path: Path | None` to `config_dir: Path | None`.

## 6.7 watcher.py

**make_notification_consumer (line 172-183):**

```python
# Before
def make_notification_consumer(
    config_path: Path | None = None,
    ...
) -> SessionEventConsumer:
    ...
    config = read_config(config_path)

# After
def make_notification_consumer(
    config_dir: Path | None = None,
    ...
) -> SessionEventConsumer:
    ...
    config = read_config(config_dir)
```

Import update (line 23): `from punt_vox.config import read_config` — unchanged (no path import needed).

### 6.8 providers/**init**.py

**get_provider (line 145-179):**

```python
# Before
def get_provider(
    name: str | None = None,
    config_path: Path | None = None,
    **kwargs: str | None,
) -> TTSProvider:
    ...
    config = read_config(config_path=config_path)

# After
def get_provider(
    name: str | None = None,
    config_dir: Path | None = None,
    **kwargs: str | None,
) -> TTSProvider:
    ...
    config = read_config(config_dir=config_dir)
```

Called from `voxd.py` (lines 1177, 1336, 1673) with `config_path=None` — rename the kwarg to `config_dir=None`. Also called from `watcher.py` line 210 with no argument (uses default) — no change needed.

## 7. .gitignore Changes

```gitignore
# Before
# Per-repo vox session state (config, ephemeral audio)
.punt-labs/vox/

# After
# Per-repo vox ephemeral state (session config, audio)
.punt-labs/vox/vox.local.md
.punt-labs/vox/ephemeral/
```

This tracks everything under `.punt-labs/vox/` except the two ephemeral paths. The durable `vox.md` becomes tracked.

Also remove `.vox/config.md` from git tracking:

```bash
git rm --cached .vox/config.md
# Then remove .vox/ directory if empty
```

## 8. Initial Tracked vox.md Content

The file ships with sensible defaults — a user who clones the repo gets working vox without creating any config:

```yaml
---
notify: "n"
speak: "y"
vibe_mode: "auto"
---
```

No `voice`, `provider`, or `model` — those default to auto-detection at runtime. The YAML frontmatter format is preserved for consistency with the existing reader.

## 9. MCP Server Instructions Update

The MCP server instructions (line 37 of server.py) reference `.punt-labs/vox/config.md`. Update to:

```python
"Do NOT use Read, Write, or Bash tools to access "
".punt-labs/vox/vox.md or .punt-labs/vox/vox.local.md. "
"All config state is available through MCP tools or hook context."
```

## 10. Test Strategy

### Unit tests: config.py

Test the core routing logic in isolation. No I/O mocking needed — use `tmp_path`.

1. **`test_write_field_routes_durable_to_vox_md`** — write `voice`, read back from `vox.md` file, confirm `vox.local.md` untouched.
2. **`test_write_field_routes_ephemeral_to_vox_local_md`** — write `vibe_signals`, confirm it lands in `vox.local.md`, `vox.md` untouched.
3. **`test_write_fields_mixed_keys_routes_correctly`** — pass `{"notify": "y", "vibe_tags": "[calm]"}`, verify `notify` in `vox.md` and `vibe_tags` in `vox.local.md`.
4. **`test_read_config_merges_both_files`** — write durable fields to `vox.md`, ephemeral to `vox.local.md`, verify `read_config` returns all.
5. **`test_read_config_ephemeral_wins_on_conflict`** — write same key to both files, verify ephemeral value returned. (Defensive — should not happen in practice.)
6. **`test_read_config_missing_files`** — neither file exists, verify safe defaults.
7. **`test_read_config_only_durable`** — only `vox.md` exists, ephemeral fields get defaults.
8. **`test_read_config_only_ephemeral`** — only `vox.local.md` exists, durable fields get defaults.
9. **`test_read_field_durable_key`** — `read_field("voice", ...)` reads from `vox.md`.
10. **`test_read_field_ephemeral_key`** — `read_field("vibe_signals", ...)` reads from `vox.local.md`.
11. **`test_write_field_creates_dir`** — write to nonexistent dir, verify dir created.
12. **`test_write_field_rejects_unknown_key`** — verify `ValueError` for unknown key.

### Unit tests: dirs.py

1. **`test_find_config_dir_walks_up`** — create `.punt-labs/vox/vox.md` in a parent, call `find_config_dir` from a child, verify directory found.
2. **`test_find_config_dir_finds_ephemeral_only`** — directory with only `vox.local.md`, verify found.
3. **`test_find_config_dir_no_legacy`** — create `.vox/config.md`, verify `find_config_dir` does NOT find it.

### Integration tests: hooks.py

1. **`test_post_bash_writes_to_vox_local_md`** — `handle_post_bash` writes `vibe_signals` to `vox.local.md`, not `vox.md`.
2. **`test_stop_hook_writes_tags_to_vox_local_md`** — `handle_stop` writes `vibe_tags` and clears `vibe_signals` in `vox.local.md`.
3. **`test_session_end_clears_signals_in_vox_local_md`** — `handle_session_end` clears `vibe_signals` in `vox.local.md`.

### Integration tests: CLI (**main**.py)

1. **`test_vibe_cmd_writes_mixed`** — `vox vibe auto` writes `vibe_mode` to `vox.md` and clears `vibe`/`vibe_tags` in `vox.local.md`.
2. **`test_notify_cmd_writes_durable`** — `vox notify y` writes to `vox.md` only.
3. **`test_voice_cmd_writes_durable`** — `vox voice fin` writes to `vox.md` only.

### Existing test updates

All existing tests that pass `config_path=` to config functions switch to `config_dir=`. Most tests already use `tmp_path` — they need to create `tmp_path / "vox.md"` and/or `tmp_path / "vox.local.md"` instead of `tmp_path / "config.md"`.

## 11. Migration

No migration. Per the contract: no users yet, clean break.

- Delete `.vox/config.md` from git (`git rm .vox/config.md`).
- Remove the `.vox/` directory.
- Add `.punt-labs/vox/vox.md` to git with initial content from section 8.
- All existing local `.punt-labs/vox/config.md` files (created by hooks at runtime) are already gitignored and will be orphaned — they are harmless. The new code does not read `config.md` from the new path.

The doctor command's legacy `.vox/` check is removed entirely.

## 12. Implementation Scope

All changes fit a single PR touching these files:

| File | Nature of change |
|------|------------------|
| `src/punt_vox/dirs.py` | Rename path constant, rename `find_config` -> `find_config_dir`, drop legacy |
| `src/punt_vox/config.py` | Split read/write routing, new constants, parameter rename, update `__all__` |
| `src/punt_vox/hooks.py` | Parameter rename throughout, drop vestigial existence guards |
| `src/punt_vox/server.py` | Parameter rename, update instructions string |
| `src/punt_vox/__main__.py` | Parameter rename, `first_init` field check, remove legacy doctor checks |
| `src/punt_vox/resolve.py` | Parameter rename |
| `src/punt_vox/watcher.py` | Parameter rename (`config_path` -> `config_dir`) |
| `src/punt_vox/providers/__init__.py` | Parameter rename (`config_path` -> `config_dir`) |
| `.gitignore` | Replace blanket `.punt-labs/vox/` ignore with specific excludes |
| `.punt-labs/vox/vox.md` | New tracked file (initial config) |
| `.vox/config.md` | Deleted from git |
| `tests/test_config.py` | New routing tests, update existing tests |
| `tests/test_hooks.py` | Update to use `config_dir` |
| `tests/test_server.py` | Update to use `config_dir` |
| `tests/test_cli.py` | Update to use `config_dir`, add `first_init` test |
| `tests/test_watcher.py` | Update to use `config_dir` |
| `tests/conftest.py` | Update shared fixtures |
