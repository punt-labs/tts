"""Pins for the fork's hook wiring and permissions rendering.

The field inventory is only complete if EVERY hook event relays; a missing
entry in the settings document is a silent hole in the evidence. These
tests pin the full-event wiring, the matcher placement, and the relay
script's stamp-then-relay pipeline.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import cast

from wiring import (
    CONTEXT_CAPTURE_V1,
    RELAYED_EVENTS,
    HookWiring,
    RelayScript,
    SettingsDocument,
)

_SCRIPT = Path("/opt/harness/relay with space.sh")

# The settings block is a wire shape (json.dumps consumes it); tests narrow
# it back to the nested dict/list form the Claude Code schema requires.
type _Entry = dict[str, object]


def _entries(event: str) -> list[_Entry]:
    settings = HookWiring(_SCRIPT).to_settings()
    return cast("list[_Entry]", settings[event])


class TestHookWiring:
    """Every event wired, matchers only where Claude Code accepts them."""

    def test_every_relayed_event_has_exactly_one_entry(self) -> None:
        settings = HookWiring(_SCRIPT).to_settings()
        assert set(settings) == set(RELAYED_EVENTS)
        assert all(len(_entries(event)) == 1 for event in RELAYED_EVENTS)

    def test_tool_events_carry_a_wildcard_matcher_and_others_do_not(self) -> None:
        for event in RELAYED_EVENTS:
            entry = _entries(event)[0]
            if event in ("PreToolUse", "PostToolUse"):
                assert entry["matcher"] == "*"
            else:
                assert "matcher" not in entry

    def test_command_quotes_the_script_path_and_passes_the_event(self) -> None:
        command = HookWiring(_SCRIPT).command_for("PostToolUse")
        assert command == "'/opt/harness/relay with space.sh' PostToolUse"

    def test_every_hook_has_a_timeout(self) -> None:
        # A dead store must not stall the captured session indefinitely.
        for event in RELAYED_EVENTS:
            hooks = cast("list[dict[str, object]]", _entries(event)[0]["hooks"])
            timeout = hooks[0]["timeout"]
            assert isinstance(timeout, int)
            assert timeout > 0


class TestRelayScript:
    """The rendered wrapper stamps sender-side, then relays."""

    def test_pipeline_captures_start_then_stamps_then_relays(self) -> None:
        body = RelayScript(
            proxy=Path("/usr/bin/mcp-proxy"),
            url="ws://127.0.0.1:9000",
            stamper=Path("/opt/harness/relay_stamp.py"),
            counter_dir=Path("/opt/harness/counters"),
        ).render()
        assert body.startswith("#!/bin/sh\n")
        assert "start_ns=$(date +%s%N)" in body
        stamp_pos = body.index("relay_stamp.py")
        proxy_pos = body.index("mcp-proxy")
        assert stamp_pos < proxy_pos  # stamp BEFORE relay, or latency is a lie
        assert '--hook "$1"' in body

    def test_interpreter_path_is_baked_absolute(self) -> None:
        # Hook commands run with no environment the harness controls, so
        # the stamper's interpreter cannot be left to a PATH lookup.
        body = RelayScript(
            proxy=Path("/usr/bin/mcp-proxy"),
            url="ws://127.0.0.1:9000",
            stamper=Path("/opt/harness/relay_stamp.py"),
            counter_dir=Path("/opt/harness/counters"),
        ).render()
        assert f"stamped=$({shlex.quote(sys.executable)} " in body
        assert "$(python3 " not in body

    def test_paths_with_spaces_are_quoted(self) -> None:
        body = RelayScript(
            proxy=Path("/opt/my tools/mcp-proxy"),
            url="ws://127.0.0.1:9000",
            stamper=Path("/opt/my tools/relay_stamp.py"),
            counter_dir=Path("/opt/my tools/counters"),
        ).render()
        assert "'/opt/my tools/mcp-proxy'" in body
        assert "'/opt/my tools/relay_stamp.py'" in body
        assert "'/opt/my tools/counters'" in body


class TestSettingsDocument:
    """The deposited settings.json is valid and carries both blocks."""

    def test_renders_valid_json_with_permissions_and_hooks(self) -> None:
        document = SettingsDocument(CONTEXT_CAPTURE_V1, HookWiring(_SCRIPT))
        parsed = cast("dict[str, object]", json.loads(document.render()))
        assert set(parsed) == {"permissions", "hooks"}
        hooks = cast("dict[str, object]", parsed["hooks"])
        assert set(hooks) == set(RELAYED_EVENTS)

    def test_profile_allows_the_work_loop_and_denies_egress(self) -> None:
        permissions = CONTEXT_CAPTURE_V1.to_settings()
        allow = cast("list[str]", permissions["allow"])
        deny = cast("list[str]", permissions["deny"])
        assert "Bash" in allow  # the test loop needs a shell
        for tool in ("WebFetch", "WebSearch", "Task"):
            assert tool in deny
        assert not set(allow) & set(deny)  # no tool both allowed and denied
