# Package Structure Analysis

Date: 2026-05-14
Author: Ralph Johnson (rej)
Status: GO

## Conclusion

**Do not create new packages. Fix three coupling defects.**

The flat layout of 27 modules is defensible after analysis. The modules
have clean dependency direction (types → platform → config → client →
presentation), are small enough to hold in mind, and don't form clusters
with strong enough internal cohesion to justify packaging overhead.

The real problems are three coupling defects that make the dependency
graph dirtier than necessary.

## Coupling Defects

### Defect 1: VOX_DATA_DIR in wrong module

`cache.py`, `keys.py`, and `playback.py` import `VOX_DATA_DIR` from
`logging_config.py`. This constant is `user_state_dir()` — a path
concern, not a logging concern. Three modules have a false dependency
on the logging module.

**Fix**: Move `VOX_DATA_DIR` to `paths.py` (where `user_state_dir()` is
defined). Update 4 importers. Zero behavior change.

### Defect 2: watcher.py bypasses voxd

`_announce_voice()` does in-process synthesis:
```python
from punt_vox.core import TTSClient
from punt_vox.providers import get_provider
provider = get_provider()
client = TTSClient(provider)
client.synthesize(request, output_path)
```

This violates the daemon/client boundary (CLAUDE.md): "No business logic
in the client layer." It creates coupling from the watcher (a hook/client
module) into `core.py` and `providers/` (synthesis/daemon modules).

**Fix**: Route through `VoxClientSync().synthesize(phrase)` like every
other client. Eliminates 3 imports, enforces the boundary.

### Defect 3: doctor.py ↔ __main__.py circular dependency

`doctor.py` lazily imports `from punt_vox.__main__ import app` to
enumerate CLI subcommands. `__main__.py` lazily imports `doctor.py`.
Circular.

**Fix**: Pass the subcommand set as a parameter to `DoctorCheck` at the
call site. Breaks the cycle.

## Dependency Graph (Actual)

```
Layer 0: TYPES (imported by everything, import nothing)
  types.py, types_audio.py, types_errors.py, types_synthesis.py
  quips.py, mood.py, music.py, normalize.py

Layer 1: PLATFORM (import nothing from punt_vox)
  paths.py, dirs.py

Layer 2: INFRASTRUCTURE
  logging_config.py → paths
  keys.py → paths (after fix 1)
  cache.py → paths (after fix 1)

Layer 3: CONFIG
  config.py → dirs
  resolve.py → config, types
  output.py → dirs, types

Layer 4: CLIENT
  client.py → paths

Layer 5: CONTENT (pure data, parallel to layers 1-4)
  voices.py → types

Layer 6: SYNTHESIS
  core.py → types

Layer 7: SUBSYSTEMS
  providers/ → types, config
  voxd/ → types, providers, config, cache, keys, normalize, music, paths
  service/ → paths

Layer 8: PRESENTATION
  server.py → client, config, types_synthesis, voices, logging_config
  hooks.py → client, config, dirs, quips
  watcher.py → config, client, hooks, mood (after fix 2)
  doctor.py → client, dirs, paths (after fix 3)
  __main__.py → client, config, dirs, hooks, paths, providers, types_synthesis
```

Clean layered structure after the three fixes. No cycles. No upward
arrows.

## What NOT to Package

- **types/ package**: The types.py facade + 3 satellites already work.
  Packaging adds import churn, no benefit.
- **content/ package**: mood, music, quips, normalize, voices don't
  import each other and don't change together. Categorization ≠ cohesion.
- **config/ package**: config.py + resolve.py + output.py have low
  internal coupling. resolve.py and output.py are 145 and 33 lines.
- **hooks/ package**: Blocked by watcher coupling defect. Fix first.
- **infra/ package**: paths, dirs, logging_config are already leaves.
  Packaging adds a level of indirection with no cognitive benefit.

## When Packages Become Worthwhile

A package boundary is worthwhile when it lets you *stop thinking about*
the modules inside. None of the candidate clusters achieve that — the
modules are small and independent enough that the boundary would be
administrative overhead rather than cognitive relief.

The three coupling fixes have immediate value: they simplify the graph,
enforce the documented daemon/client boundary, and break a cycle.
