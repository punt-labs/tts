"""Permissions profile, relay-script rendering, and hook wiring for the fork.

Adapted from the frozen vox-juhw spike's ``profiles.py`` with two deliberate
changes for this spike's measurand -- payload richness under REAL work:

1. **Every hook event Claude Code fires is wired**, not the five juhw
   needed: the field inventory must show what each event type carries, so
   the wiring cannot pre-filter.
2. **Each hook command routes through a rendered relay script** that stamps
   the payload sender-side (``relay_stamp.py``: per-session ``relay_seq``
   for gap detection, ``relay_start_ns`` for latency) before handing it to
   the real ``mcp-proxy --hook``.

The profile also allows Bash, which juhw's voice-launch profile denied: the
captured session must run a real test-failure/debug/fix loop, and running
tests takes a shell. This fork is a stand-in for the USER'S OWN working
session (the thing whose hooks feed DES-070's store), not a voice-launched
capability grant, so the juhw blast-radius argument does not transfer.
Isolation still holds: scratch git project, fresh CLAUDE_CONFIG_DIR,
blanked API keys, network tools denied, teardown removes everything.
"""

from __future__ import annotations

import json
import shlex
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from pathlib import Path

# Every hook event the fork can fire. PreToolUse/PostToolUse take a tool
# matcher; the rest are bare. SubagentStart/Stop stay wired even though the
# profile denies Task -- absence in the ledger is then evidence, not a hole
# in the wiring.
RELAYED_EVENTS: tuple[str, ...] = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Notification",
    "Stop",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "SessionEnd",
)

# Events whose settings entry takes a tool matcher.
_MATCHER_EVENTS = frozenset({"PreToolUse", "PostToolUse"})

# Seconds Claude Code allows one relay command before killing it. Keeps a
# dead store from stalling the session longer than this per hook fire.
_HOOK_TIMEOUT_S = 10


@final
@dataclass(frozen=True, slots=True)
class PermissionsProfile:
    """A named, curated tool surface the spawned session inherits."""

    name: str
    allow: tuple[str, ...]
    deny: tuple[str, ...]
    default_mode: str

    def to_settings(self) -> dict[str, object]:
        """Return the `permissions` block for `.claude/settings.json`."""
        return {
            "allow": list(self.allow),
            "deny": list(self.deny),
            "defaultMode": self.default_mode,
        }


# The realism-capture profile: file tools plus Bash for the test loop;
# network egress and sub-agent spawning stay denied.
CONTEXT_CAPTURE_V1 = PermissionsProfile(
    name="context-capture-v1",
    allow=("Read", "Write", "Edit", "Glob", "Grep", "TodoWrite", "Bash"),
    deny=("WebFetch", "WebSearch", "Task", "NotebookEdit"),
    default_mode="acceptEdits",
)


@final
class RelayScript:
    """Renders the shell wrapper each hook command runs.

    The script captures the hook-command start time as early as a shell
    can (``date +%s%N``), pipes the payload through the sender-side
    stamper, and hands the stamped JSON to the real ``mcp-proxy --hook``.
    All paths are baked in absolute at render time -- including the
    Python interpreter (``sys.executable``) -- because hook commands run
    with no environment the harness controls, so even ``python3`` cannot
    be left to a PATH lookup.
    """

    __slots__ = ("_counter_dir", "_proxy", "_stamper", "_url")

    _counter_dir: Path
    _proxy: Path
    _stamper: Path
    _url: str

    def __new__(cls, proxy: Path, url: str, stamper: Path, counter_dir: Path) -> Self:
        self = super().__new__(cls)
        self._proxy = proxy
        self._url = url
        self._stamper = stamper
        self._counter_dir = counter_dir
        return self

    def render(self) -> str:
        """The relay script body; takes the event name as $1.

        The stamper runs in a command substitution, NOT a pipe: POSIX sh
        has no pipefail, so a piped stamper crash (bad stdin, unwritable
        counter dir) would vanish behind the proxy's exit 0 -- the event
        dropped silently AND the sender sequence never advanced, leaving
        gap detection structurally blind to it. The substitution makes
        the stamper's exit status the hook's own, so the loss is loud in
        the fork's session log.
        """
        python = shlex.quote(sys.executable)
        stamper = shlex.quote(str(self._stamper))
        counters = shlex.quote(str(self._counter_dir))
        proxy = shlex.quote(str(self._proxy))
        url = shlex.quote(self._url)
        return (
            "#!/bin/sh\n"
            "# Rendered by the vox-73y7 harness: sender-side stamp, then relay.\n"
            "start_ns=$(date +%s%N)\n"
            f"stamped=$({python} {stamper} --counter-dir {counters} "
            '--start-ns "$start_ns") || exit 1\n'
            f'printf \'%s\' "$stamped" | {proxy} {url} --hook "$1"\n'
        )


@final
class HookWiring:
    """Builds the hooks block: every event relays through the stamped script."""

    __slots__ = ("_script",)

    _script: Path

    def __new__(cls, script: Path) -> Self:
        self = super().__new__(cls)
        self._script = script
        return self

    def command_for(self, event: str) -> str:
        """The exact shell command Claude Code runs for one hook event."""
        return f"{shlex.quote(str(self._script))} {shlex.quote(event)}"

    def to_settings(self) -> dict[str, object]:
        """Return the `hooks` block for `.claude/settings.json`."""
        return {event: [self._entry_for(event)] for event in RELAYED_EVENTS}

    def _entry_for(self, event: str) -> dict[str, object]:
        entry: dict[str, object] = {
            "hooks": [
                {
                    "type": "command",
                    "command": self.command_for(event),
                    "timeout": _HOOK_TIMEOUT_S,
                }
            ]
        }
        if event in _MATCHER_EVENTS:
            entry["matcher"] = "*"
        return entry


@final
class SettingsDocument:
    """The `.claude/settings.json` the launcher deposits in the fork's project."""

    __slots__ = ("_profile", "_wiring")

    _profile: PermissionsProfile
    _wiring: HookWiring

    def __new__(cls, profile: PermissionsProfile, wiring: HookWiring) -> Self:
        self = super().__new__(cls)
        self._profile = profile
        self._wiring = wiring
        return self

    def render(self) -> str:
        """Serialize the full settings document."""
        return json.dumps(
            {
                "permissions": self._profile.to_settings(),
                "hooks": self._wiring.to_settings(),
            },
            indent=2,
            sort_keys=True,
        )
