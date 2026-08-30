"""The deposited settings document: profile shape and loopback hook wiring."""

from __future__ import annotations

import json
from pathlib import Path

from profiles import (
    RELAYED_EVENTS,
    VOICE_LAUNCH_V1,
    HookWiring,
    PermissionsProfile,
    SettingsDocument,
)

_PROXY = Path("/usr/local/bin/mcp-proxy")
_URL = "ws://127.0.0.1:8931"


class TestPermissionsProfile:
    """The curated tool surface DES-071 names as the escalation mitigation."""

    def test_v1_profile_denies_shell_and_network_tools(self) -> None:
        assert "Bash" in VOICE_LAUNCH_V1.deny
        assert "WebFetch" in VOICE_LAUNCH_V1.deny
        assert "WebSearch" in VOICE_LAUNCH_V1.deny

    def test_v1_profile_allows_only_file_scoped_tools(self) -> None:
        assert set(VOICE_LAUNCH_V1.allow) <= {
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "TodoWrite",
        }

    def test_settings_block_carries_all_three_axes(self) -> None:
        profile = PermissionsProfile("p", ("Read",), ("Bash",), "plan")
        block = profile.to_settings()
        assert block == {
            "allow": ["Read"],
            "deny": ["Bash"],
            "defaultMode": "plan",
        }


class TestHookWiring:
    """Every relayed event routes through the real mcp-proxy to the store."""

    def test_command_is_the_mcp_proxy_hook_shape(self) -> None:
        wiring = HookWiring(_PROXY, _URL)
        assert wiring.command_for("Stop") == (
            "/usr/local/bin/mcp-proxy ws://127.0.0.1:8931 --hook Stop"
        )

    def test_all_relayed_events_are_wired(self) -> None:
        block = HookWiring(_PROXY, _URL).to_settings()
        assert set(block) == set(RELAYED_EVENTS)

    def test_tool_events_carry_a_wildcard_matcher(self) -> None:
        block = HookWiring(_PROXY, _URL).to_settings()
        post = block["PostToolUse"]
        assert isinstance(post, list)
        assert post[0]["matcher"] == "*"
        assert "matcher" not in json.dumps(block["SessionStart"])

    def test_every_hook_command_is_time_bounded(self) -> None:
        block = HookWiring(_PROXY, _URL).to_settings()
        for event in RELAYED_EVENTS:
            entries = block[event]
            assert isinstance(entries, list)
            hooks = entries[0]["hooks"]
            assert all(h["timeout"] > 0 for h in hooks)


class TestSettingsDocument:
    """The rendered `.claude/settings.json` is one parseable document."""

    def test_render_combines_permissions_and_hooks(self) -> None:
        doc = SettingsDocument(VOICE_LAUNCH_V1, HookWiring(_PROXY, _URL))
        parsed = json.loads(doc.render())
        assert parsed["permissions"]["defaultMode"] == "acceptEdits"
        assert set(parsed["hooks"]) == set(RELAYED_EVENTS)
        for event in RELAYED_EVENTS:
            command = parsed["hooks"][event][0]["hooks"][0]["command"]
            assert command.endswith(f"--hook {event}")
