# Phase 1 Step 4: HookPayload Design

Date: 2026-05-15
Status: REVISED — peer review by rej 2026-05-15
Bead: vox-ek73

## Problem

Three hook handlers receive `data: dict[str, object]` and immediately do
`data.get("key", default)` + `isinstance` guards to extract typed fields:

- `handle_stop` (line 237): `data.get("stop_hook_active", False)`
- `handle_post_bash` (lines 325-338): `data.get("tool_response", {})`,
  `exit_code_raw`, `stdout` — multiple isinstance guards
- `handle_notification` (lines 398-403): `data.get("notification_type")`,
  `data.get("message")`

Other handlers (pre_compact, session_end, user_prompt_submit) take `VoxConfig`
only — no stdin payload. They are not touched.

The three data-carrying payloads have different shapes with no enforcement.

## Proposed Types

In a new file `src/punt_vox/hook_payload.py`:

```python
@dataclass(frozen=True, slots=True)
class StopPayload:
    stop_hook_active: bool

@dataclass(frozen=True, slots=True)
class BashPayload:
    exit_code: int | None
    stdout: str

@dataclass(frozen=True, slots=True)
class NotificationPayload:
    notification_type: str
    message: str

HookPayload = StopPayload | BashPayload | NotificationPayload
```

And a `parse_hook_payload(data: dict[str, object], kind: str) -> HookPayload`
function that dispatches on `kind` ("stop", "post_bash", "notification") and
extracts/validates the relevant fields. The isinstance guards and `.get()`
calls move inside the parse function.

## Updated handler signatures

```python
def handle_stop(payload: StopPayload, config: VoxConfig) -> dict[str, object] | None:
    stop_active = payload.stop_hook_active  # was: data.get("stop_hook_active", False)

def handle_post_bash(payload: BashPayload, config_dir: Path) -> None:
    exit_code = payload.exit_code  # was: data.get("tool_response", {})...

def handle_notification(payload: NotificationPayload, config: VoxConfig) -> None:
    notification_type = payload.notification_type  # was: data.get("notification_type")
    message = payload.message                       # was: data.get("message")
```

## Entry point changes

The CLI entry-point commands are in `hooks.py` (not `__main__.py`):
`stop_cmd`, `post_bash_cmd`, and `notification_cmd` (lines ~556, ~569, ~579).
`__main__.py` only imports `hook_app` from `hooks.py` — it requires no changes.

Current flow in each command:
1. Call `_read_hook_input()` to get `dict[str, object]`
2. Pass the dict to the handler

After the change:
1. Call `_read_hook_input()` to get `dict[str, object]`
2. Call `parse_hook_payload(data, kind)` to get a typed payload
3. Pass the typed payload to the handler

## parse_hook_payload implementation notes

**BashPayload — nested tool_response**: `handle_post_bash` reads from a
nested structure. `parse_hook_payload` for `"post_bash"` must:
```python
tool_response = data.get("tool_response", {})
exit_code_raw = tool_response.get("exit_code") if isinstance(tool_response, dict) else None
stdout = tool_response.get("stdout", "") if isinstance(tool_response, dict) else ""
```
Then coerce `exit_code_raw` to `int` with try/except (current code does this).
Direct `data.get("exit_code")` is WRONG — the field is nested.

**StopPayload — bool coercion**: `handle_stop` checks `stop_active is True`
(identity, not equality). `parse_hook_payload` must coerce:
```python
stop_hook_active = bool(data.get("stop_hook_active", False))
```
This ensures a truthy non-bool value doesn't slip through. The field in the
dataclass is typed `bool`, enforcing this at the parse boundary.

## Invariants

1. **No behavior change.** The parse functions extract the same values with
   the same defaults and fallbacks as the existing `.get()` + isinstance code.

2. **BashPayload.stdout default**: `handle_post_bash` currently defaults
   `stdout` to empty string when missing. `BashPayload` stores the resolved
   value; `parse_hook_payload` applies the default.

3. **Three payload types only.** Other hook handlers (pre_compact, etc.) do
   not have a data payload and are not touched.

4. **`_read_hook_input()` unchanged.** It returns `dict[str, object]`; the
   parse function is called by the entry point after reading.

## Files Changed

- `src/punt_vox/hook_payload.py` (new): `StopPayload`, `BashPayload`,
  `NotificationPayload`, `parse_hook_payload(data, kind)`
- `src/punt_vox/hooks.py`: update the three handler signatures; update the
  three entry-point commands (`stop_cmd`, `post_bash_cmd`, `notification_cmd`)
  to call `parse_hook_payload` before calling the handler; remove inline
  `.get()` + isinstance guards from handler bodies
- `src/punt_vox/__main__.py`: NO CHANGES — it only imports `hook_app`
- `tests/test_hooks.py`: update tests that pass dicts directly to handlers;
  add tests for `parse_hook_payload` including nested tool_response extraction

## Completion Test

```bash
grep -rn "data\.get\|data\[" src/punt_vox/hooks.py | grep -v "def \|#"
# Must return zero hits inside the three updated handlers
make check
```
