"""Permissions profile and hook wiring deposited into the fork's settings.

DES-071 names `permissions_profile` as the mitigation for launch-as-capability
-escalation: the spawned session inherits a curated tool surface, not the
launcher's full authority. The concrete mechanism validated here is a project
`.claude/settings.json` written by the launcher before the fork: its
`permissions` block carries the profile (allow list, deny list, default mode)
and its `hooks` block routes every relayed event through the real
`mcp-proxy --hook <Event>` to the stub voxd store over loopback WebSocket.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from pathlib import Path

# Events the spawned session relays back to the store. SessionStart proves
# the fork configured itself; UserPromptSubmit proves the seed prompt landed;
# PostToolUse proves mid-run payloads flow; Stop proves turn completion; and
# SessionEnd proves teardown is observable.
RELAYED_EVENTS: tuple[str, ...] = (
    "SessionStart",
    "UserPromptSubmit",
    "PostToolUse",
    "Stop",
    "SessionEnd",
)

# Events whose settings entry takes a tool matcher.
_MATCHER_EVENTS = frozenset({"PostToolUse"})

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


# The v1 profile under test: file tools inside the scratch project only.
# Bash is denied outright -- a voice-launched session that needs a shell is
# beyond v1's blast radius; edits auto-accept only inside the project dir.
VOICE_LAUNCH_V1 = PermissionsProfile(
    name="voice-launch-v1",
    allow=("Read", "Write", "Edit", "Glob", "Grep", "TodoWrite"),
    deny=("Bash", "WebFetch", "WebSearch", "Task", "NotebookEdit"),
    default_mode="acceptEdits",
)


@final
class HookWiring:
    """Builds the hooks block: each event relays to the store via mcp-proxy."""

    __slots__ = ("_proxy", "_url")

    _proxy: Path
    _url: str

    def __new__(cls, proxy: Path, url: str) -> Self:
        self = super().__new__(cls)
        self._proxy = proxy
        self._url = url
        return self

    def command_for(self, event: str) -> str:
        """The exact shell command Claude Code runs for one hook event."""
        return f"{self._proxy} {self._url} --hook {event}"

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
