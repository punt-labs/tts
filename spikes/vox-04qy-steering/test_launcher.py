"""Pins for the launcher's pure argv construction (no tmux spawned).

A wrong quote or a dropped env entry here launches a fork that half-works:
the session starts but reads the user's real config, or the prompt splits
at the first space. These pins hold the argv exactly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from launcher import SESSION_PREFIX, LaunchCommand, TmuxSession


class TestLaunchCommand:
    """The exact shell line handed to the fork's pane."""

    def test_empty_prompt_is_refused(self) -> None:
        with pytest.raises(ValueError, match="empty prompt"):
            LaunchCommand(Path("/usr/local/bin/claude"), "")

    def test_prompt_travels_as_one_quoted_argument(self) -> None:
        command = LaunchCommand(Path("/usr/local/bin/claude"), "fix the bug; run tests")
        assert command.to_shell() == ("/usr/local/bin/claude 'fix the bug; run tests'")

    def test_binary_path_with_spaces_is_quoted(self) -> None:
        command = LaunchCommand(Path("/opt/my tools/claude"), "hi")
        assert command.to_shell().startswith("'/opt/my tools/claude' ")


class TestSpawnArgv:
    """The tmux invocation: detached, named, cwd-pinned, env-injected."""

    def test_argv_shape(self, tmp_path: Path) -> None:
        session = TmuxSession(f"{SESSION_PREFIX}-t1")
        argv = session.spawn_argv(
            LaunchCommand(Path("/bin/claude"), "go"),
            cwd=tmp_path,
            env={"CLAUDE_CONFIG_DIR": "/cfg", "ANTHROPIC_API_KEY": ""},
        )
        assert argv[:5] == ["tmux", "new-session", "-d", "-s", f"{SESSION_PREFIX}-t1"]
        assert argv[5:7] == ["-c", str(tmp_path)]
        # env entries are sorted and each rides its own -e flag
        assert argv[7:11] == [
            "-e",
            "ANTHROPIC_API_KEY=",
            "-e",
            "CLAUDE_CONFIG_DIR=/cfg",
        ]
        assert argv[-1] == "/bin/claude go"

    def test_every_env_entry_is_injected(self, tmp_path: Path) -> None:
        env = {"A": "1", "B": "2", "C": "3"}
        argv = TmuxSession("s").spawn_argv(
            LaunchCommand(Path("/bin/claude"), "go"), cwd=tmp_path, env=env
        )
        flags = [argv[i + 1] for i, item in enumerate(argv) if item == "-e"]
        assert flags == ["A=1", "B=2", "C=3"]


class TestInjectionArgv:
    """The send mechanics under test in the matrix, pure and assertable."""

    def test_literal_argv_uses_dash_l_and_guards_leading_dash(self) -> None:
        session = TmuxSession(f"{SESSION_PREFIX}-t2")
        argv = session.literal_argv("-starts with a dash")
        assert argv == [
            "tmux",
            "send-keys",
            "-t",
            f"{SESSION_PREFIX}-t2:",
            "-l",
            "--",
            "-starts with a dash",
        ]

    def test_paste_argv_pair_is_bracketed_and_buffer_scoped(self) -> None:
        session = TmuxSession(f"{SESSION_PREFIX}-t3")
        load_argv, paste_argv = session.paste_argv("bufname")
        assert load_argv == ["tmux", "load-buffer", "-b", "bufname", "-"]
        assert paste_argv == [
            "tmux",
            "paste-buffer",
            "-p",
            "-d",
            "-b",
            "bufname",
            "-t",
            f"{SESSION_PREFIX}-t3:",
        ]


class TestExtraEnv:
    """extra_env entries must ride the same tmux -e injection."""

    def test_extra_env_merges_over_config_env(self, tmp_path: Path) -> None:
        # spawn_argv is the pure seam: build the merged env the way
        # SessionLauncher.launch does and assert the stub PATH survives.
        session = TmuxSession(f"{SESSION_PREFIX}-t4")
        merged = {"CLAUDE_CONFIG_DIR": "/cfg", "PATH": "/stubs:/usr/bin"}
        argv = session.spawn_argv(
            LaunchCommand(Path("/bin/claude"), "go"), cwd=tmp_path, env=merged
        )
        assert "PATH=/stubs:/usr/bin" in argv
