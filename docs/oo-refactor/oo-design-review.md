# Refactoring Plan Review -- GO/NO-GO Gate

Reviewer: Ralph Johnson (rej)
Date: 2026-05-13
Document reviewed: `docs/oo-refactor/oo-refactoring-plan.md` (52 steps across Parts 2 and 3)
Tool: `tools/oo_score.py` (11 metrics, 33 source files)

---

## 1. Safety Assessment: STOP (conditional -- fixable)

The plan's design is sound. The dependency graph is acyclic, the phased
extraction follows the correct order, the DaemonContext delegation strategy is
the right way to do an incremental migration, and the risk mitigations are
realistic. However, there are two safety issues that must be corrected before
execution begins.

### 1a. Part 2 uses `__init__` everywhere; the OO gate requires `__new__`

Steps 1 through 17 show constructor examples using `def __init__`. The
`init_violations` metric counts every `__init__` on a non-dataclass class as a
violation. If an implementer follows the plan literally, every new class in
Part 2 will introduce an init_violation:

- Step 1: `DaemonConfig.__init__`
- Step 4: `PlaybackQueue.__init__`
- Step 5: `TrackGenerator.__init__`
- Step 6: `SynthesisPipeline.__init__`
- Step 7: `MusicScheduler.__init__`
- Step 8: `DaemonHealth.__init__`
- Step 9: `WebSocketRouter.__init__`
- Step 10: `VoxDaemon.__init__`
- Step 11: `ProcessManager.__init__`
- Step 12: `KeysEnvWriter.__init__`
- Step 13: `LaunchdBackend.__init__`
- Step 14: `SystemdBackend.__init__`
- Step 15: `ServiceInstaller.__init__`
- Step 17: `DoctorCheck.__init__`

That is 14 new init_violations. There are no Part 3 steps to fix them -- Part 3
steps 23-28 fix init_violations in the *existing* provider classes, and steps
29-43 create new classes using `__new__`, but nobody fixes the 14 classes created
in Part 2.

**Fix**: Replace every `__init__` in Part 2 code examples with `__new__` using
the standard pattern:

```python
def __new__(cls, ...) -> Self:
    self = super().__new__(cls)
    self._field = value
    return self
```

This is a documentation fix, not a design change. The class specs, owned state,
and method lists are all correct -- only the constructor spelling is wrong.

### 1b. DaemonContext delegation setters bypass encapsulation

Step 4 (PlaybackQueue) shows a delegation setter on DaemonContext:

```python
@last_playback.setter
def last_playback(self, value):
    self._playback._last_result = value  # tests set this directly
```

And Step 7 (MusicScheduler):

```python
@music_mode.setter
def music_mode(self, val): self._music._mode = val
```

These reach into another object's private attributes. This is a
public_attr_violation on the delegation target and an encapsulation breach. The
comment "tests set this directly" is the tell -- the tests are the problem, not
the production code.

**Fix**: Instead of delegation setters that reach into private attributes, give
the new classes explicit setter methods or writable properties for the fields
that tests need to manipulate. For example:

- `PlaybackQueue` gets a `set_last_result(value)` method (used only in tests)
- `MusicScheduler` gets property setters on `mode`, `style`, etc.

Or better: update the tests in the same step to construct the new classes
directly instead of going through DaemonContext. The plan already says "tests
that move" for each step -- the moved tests should use the new API, not the
delegation layer.

**Risk if unfixed**: The encapsulation_ratio metric will fail for any file that
contains `self._other_object._private = value`. The OO score tool catches
`self.X = value` where X has no underscore, but even with the underscore, the
design is wrong -- a delegation layer that exposes another object's internals
defeats the purpose of extraction.

---

## 2. Completeness Check: Per-Metric Analysis

### 2.1 method_ratio >= 0.80

Verdict: INCOMPLETE for 2 files

The plan achieves this for most files by wrapping module-level functions into
classes. The pattern is applied consistently in Steps 18-43 (Part 3).

**Gap: `__main__.py`**. After Step 17 extracts DoctorCheck (~300 LOC), the file
is still ~1700 LOC with ~37 top-level functions and 0 classes. `method_ratio`
remains 0.0. No step addresses this.

**Gap: `server.py`**. After Step 16 (SessionConfig refactor), the file is
~823 LOC with ~22 MCP tool functions and 1 class. `method_ratio` stays near
0.04. Section 3.0 describes splitting into `server.py` + `server_tools.py` but
there is no numbered execution step. Even after a split, the tool functions are
still top-level functions, not methods -- `method_ratio` and
`class_to_func_ratio` remain at 0.

### 2.2 encapsulation_ratio >= 1.0

Verdict: COVERED

Step 22 fixes the `VoiceNotFoundError` public attributes. Steps 0-10 migrate
voxd.py's public attributes to private attributes on new classes. Conditional
on fix 1b above (delegation setters).

### 2.3 avg_params <= 4.0

Verdict: COVERED

No file currently fails this metric. The plan does not introduce any new
parameter bloat.

### 2.4 max_complexity <= 10

Verdict: INCOMPLETE for 2 files

The plan addresses complexity in `resolve.py` (Step 40: "Extract Method on
the function with CC=11"), `espeak.py` (Step 27: "The method with CC=14 needs
Extract Method"), and `normalize.py` (Step 19: implicit via class wrapping).

**Gap: `__main__.py` (CC=38)**. Step 17 extracts DoctorCheck but does not
address the CC=38 function (likely the `doctor` command itself or another
complex CLI command). Even if DoctorCheck absorbs it, `__main__.py` probably
retains several functions above CC=10.

**Gap: `server.py` (CC=36)**. Step 16 refactors SessionState but does not
address the function with CC=36. Section 3.0 mentions splitting but has no
numbered step and does not address complexity.

**Gap: `hooks.py` (CC=21)**. Step 18 wraps functions into HookDispatcher but
the plan text does not mention Extract Method for the CC=21 function. Wrapping
into a class does not reduce cyclomatic complexity.

**Gap: `service.py` (CC=20)**. Steps 11-15 decompose service.py into classes.
The CC=20 function needs to land in one of those classes and have Extract Method
applied. The plan does not call this out.

**Gap: `client.py` (CC=13)**. Step 21 mentions "The method with CC=13 needs
Extract Method to bring it under 10" -- this is covered.

### 2.5 avg_complexity <= 5.0

Verdict: INCOMPLETE for 2 files

- `__main__.py` (6.02): not addressed beyond DoctorCheck extraction
- `server.py` (6.36): not addressed beyond SessionConfig refactor
- `applet.py` (5.25): Step 42 wraps into class but does not address complexity

Note: `applet.py` has `avg_complexity=5.25` which exceeds the 5.0 threshold.
Step 42 wraps it into a class but moving functions into methods does not change
their complexity. Extract Method is needed on the complex functions.

### 2.6 module_size <= 300

Verdict: INCOMPLETE for 2 files

The plan is thorough here for most files. Section 3.0 correctly reconciles the
Part 2 target (500) down to the tool's threshold (300), and adds splits for
`router.py`, `synthesis.py`, and `server.py`.

**Gap: `__main__.py`**. Even after DoctorCheck extraction, ~1700 LOC remain. No
split step exists. The file needs to be decomposed into at least 6 modules to
get each under 300 LOC.

**Gap: `server.py`**. Section 3.0 describes a split but there is no numbered
step. An implementer executing steps 0-44 will skip this.

### 2.7 classes_per_module <= 3

Verdict: COVERED

Step 22 splits `types.py` (8 classes) into 3 files. Step 21 splits `client.py`
(5 classes) into 2 files. The voxd/* extraction distributes voxd.py's 7 classes
across separate modules.

### 2.8 class_to_func_ratio >= 0.5

Verdict: INCOMPLETE for 2 files

Same gaps as method_ratio: `__main__.py` and `server.py`. Every other file
is covered.

### 2.9 init_violations == 0

Verdict: INCOMPLETE

Part 3 fixes existing init_violations (providers, core.py, watcher.py, types.py,
client.py). But Part 2 *introduces* 14 new init_violations (see Section 1a).

**Missing file: `providers/elevenlabs_music.py`**. Has `init_violations=1`.
No step addresses it.

### 2.10 public_attr_violations == 0

Verdict: COVERED

Step 22 fixes `types.py` (2 violations). Steps 0-10 fix `voxd.py` (2
violations) by migrating to new classes with private attributes.

### 2.11 future_annotations == 1

Verdict: COVERED.
Step 44 adds the import to `assets/__init__.py`, the only file missing it.
All new files created in Part 2 will need it too, but that follows from the
project's standard Python conventions.

---

## 3. Missing Files

### `providers/elevenlabs_music.py`

This file has 1 failure: `init_violations=1`. The plan does not mention it
anywhere. The fix is the same pattern as Steps 23-27:
`ElevenLabsMusicProvider.__init__` -> `ElevenLabsMusicProvider.__new__`.

### `__main__.py` (effectively missing)

Step 17 extracts DoctorCheck, but `__main__.py` itself retains 5 metric
failures after the extraction: `method_ratio`, `class_to_func_ratio`,
`module_size`, `max_complexity`, `avg_complexity`. The plan treats `__main__.py`
as a CLI entry point that "does not benefit from further splitting" (implied by
the lack of additional steps), but the OO score tool does not exempt CLI files.
If the tool is the gate, `__main__.py` must pass.

---

## 4. Missing Metrics

| Metric | Files not covered |
|--------|-------------------|
| method_ratio >= 0.80 | `__main__.py`, `server.py` |
| max_complexity <= 10 | `__main__.py`, `server.py`, `hooks.py`, `service.py` |
| avg_complexity <= 5.0 | `__main__.py`, `server.py`, `applet.py` |
| module_size <= 300 | `__main__.py`, `server.py` |
| class_to_func_ratio >= 0.5 | `__main__.py`, `server.py` |
| init_violations == 0 | 14 new voxd and service classes (Part 2), `elevenlabs_music.py` |

---

## 5. Step-Level Issues

### Step 1: Mock target migration is a big-bang risk

The plan acknowledges this is HIGH risk. Changing every `patch("punt_vox.voxd.X")`
to `patch("punt_vox.voxd._monolith.X")` in a 4164-line test file in a single
commit is a large mechanical change. If any mock target is missed, the test
passes silently (the mock patches nothing, the real code runs) and the failure
surfaces later as a flaky or wrong test. This is the right thing to do, but the
implementer should verify by running each test class individually and confirming
mock targets are actually being hit.

### Step 7: MusicScheduler has 10 state fields

Moving 10 fields off DaemonContext in one step, adding 10 delegation
property-pairs, and migrating tests that set those fields -- this is the largest
single-step state migration in the plan. It would be safer to split into two
sub-steps: (a) create MusicScheduler with its methods but keep fields on
DaemonContext via delegation (tests unchanged), then (b) move tests to use
MusicScheduler directly and remove delegations. The plan combines these.

### Step 9: WebSocketRouter is the plan's critical path

All handler functions move, all DaemonContext music delegations are removed, and
the handler signatures change (dropping the `ctx` parameter). If any handler
is missed or its signature is wrong, tests break. The plan correctly identifies
this as HIGH risk. No decomposition is offered.

### Step 10: `_monolith.py` rename timing

The plan says "rename `_monolith.py` to `daemon.py`" in Step 10. This changes
every import path one more time. An alternative: name it `daemon.py` from
Step 1 (skip the `_monolith` name entirely). The original `voxd.py` becomes
`voxd/__init__.py` with re-exports, and the monolith starts life as `daemon.py`.
This avoids one rename and one round of mock-target changes.

### Steps 29-43: Wrapper classes with only staticmethods

Several of these classes (`DirectoryResolver`, `LoggingSetup`, `MoodClassifier`,
`OutputResolver`, `AudioPlayer`, `MusicPromptBuilder`) have no instance state
-- every method is a staticmethod. The class exists solely to pass the
`method_ratio` and `class_to_func_ratio` metrics. This is not wrong -- a
namespace is a legitimate use of a class -- but it is worth noting that these
classes add no encapsulation benefit. The `__new__` constructor creates an
instance that is never used. If the team later decides these metric thresholds
are too aggressive for stateless utility modules, these classes are the first
candidates for removal.

That said: the tool is the gate. These classes pass the gate. The alternative
(exempting pure-function modules from the metric) requires changing the tool,
which is outside scope.

### Steps 18-19: Module splits create new files not in the OO score baseline

Steps 18 and 19 split `hooks.py` into `hooks.py` + `hooks_cli.py`, and
`normalize.py` into `normalize.py` + `normalize_tables.py`. The new files
(`hooks_cli.py`, `normalize_tables.py`) must also pass all 11 metrics. The plan
does not verify this. `normalize_tables.py` (~400 LOC of pure dict literals) may
exceed `module_size <= 300` and will have `method_ratio=0.0` and
`class_to_func_ratio=0.0` unless the tool treats zero-function files with fewer
than 6 top-level statements as passing (which it does -- see
`_method_ratio()` logic: "if total == 0 and top_stmts <= 5: return 1.0"). But
400 lines of dict assignment will have more than 5 top-level statements, so the
tool will report failures. The plan needs to address this.

---

## 6. Verdict: REVISE

The plan's architecture is correct. The dependency graph is sound. The phased
decomposition follows the right order. The class responsibilities are
well-defined. But the plan is not ready for execution because:

1. **Part 2 constructors use `__init__`, not `__new__`** -- 14 new
   init_violations with no remediation step. Fix: change the code examples.

2. **`__main__.py` is not covered** beyond DoctorCheck extraction. 5 metrics
   remain failing. Fix: add steps to decompose `__main__.py` into smaller
   modules (e.g., CLI command groups in separate files, each under 300 LOC,
   with commands wrapped in a class or using Typer's class-based API).

3. **`server.py` has no numbered execution step** for the split described in
   Section 3.0. 5 metrics remain failing. Fix: add a concrete step (Step 45 or
   insert after Step 16) that splits server.py and wraps MCP tools.

4. **`providers/elevenlabs_music.py` is missing** from the plan. 1 metric
   fails. Fix: add a step (same pattern as Steps 23-27).

5. **Complexity (max_complexity, avg_complexity) not addressed** for
   `hooks.py`, `service.py`, `__main__.py`, `server.py`, `applet.py`. Wrapping
   functions in a class does not reduce their cyclomatic complexity. Fix: add
   Extract Method calls to each step where max_complexity exceeds 10.

6. **`normalize_tables.py` split may fail module_size and ratio metrics**.
   Fix: either keep the tables as class-level constants inside TextNormalizer
   (avoids the split) or verify the tool's scoring of a pure-data module.

These are all fixable. The plan needs a revision pass, not a redesign. The
dependency graph, class responsibilities, state migration strategy, and test
migration approach are all correct. Fix the six issues above and this is GO.

---

## Appendix: Current Per-File Failure Count

From `uv run python tools/oo_score.py src/punt_vox/ --threshold` (2026-05-13):

| File | Failures | Plan step(s) | Residual after plan |
|------|----------|--------------|---------------------|
| `__init__.py` | 0 | -- | 0 |
| `__main__.py` | 5 | Step 17 | 5 (DoctorCheck only) |
| `applet.py` | 3 | Step 42 | 1 (avg_complexity) |
| `assets/__init__.py` | 1 | Step 44 | 0 |
| `cache.py` | 2 | Step 30 | 0 |
| `client.py` | 5 | Step 21 | 0 |
| `config.py` | 2 | Step 29 | 0 |
| `core.py` | 3 | Step 43 | 0 |
| `dirs.py` | 2 | Step 31 | 0 |
| `hooks.py` | 4 | Step 18 | 1 (max_complexity) |
| `keys.py` | 2 | Step 32 | 0 |
| `logging_config.py` | 2 | Step 33 | 0 |
| `mood.py` | 2 | Step 34 | 0 |
| `music.py` | 2 | Step 35 | 0 |
| `normalize.py` | 5 | Step 19 | 0 (if split works) |
| `output.py` | 2 | Step 36 | 0 |
| `paths.py` | 2 | Step 37 | 0 |
| `playback.py` | 2 | Step 38 | 0 |
| `providers/__init__.py` | 2 | Step 28 | 0 |
| `providers/elevenlabs.py` | 4 | Step 23 | 0 |
| `providers/elevenlabs_music.py` | 1 | NONE | 1 (init_violations) |
| `providers/espeak.py` | 4 | Step 27 | 0 |
| `providers/openai.py` | 1 | Step 25 | 0 |
| `providers/polly.py` | 4 | Step 24 | 0 |
| `providers/say.py` | 3 | Step 26 | 0 |
| `quips.py` | 2 | Step 39 | 0 |
| `resolve.py` | 3 | Step 40 | 0 |
| `server.py` | 5 | Step 16 | 5 (no split step) |
| `service.py` | 4 | Steps 11-15 | 0 |
| `types.py` | 5 | Step 22 | 0 |
| `voices.py` | 2 | Step 41 | 0 |
| `voxd.py` | 8 | Steps 0-10 | 0 |
| `watcher.py` | 5 | Step 20 | 0 |

**Residual failures after all 45 steps**: 13 (across `__main__.py`, `server.py`,
`hooks.py`, `applet.py`, `elevenlabs_music.py`).

**Residual failures if the 6 revision items are applied**: 0.
