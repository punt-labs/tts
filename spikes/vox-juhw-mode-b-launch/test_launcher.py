"""Fork command construction, session naming, and the per-run fork cap.

Everything here is pure argv/shape assertion -- no tmux session is created
and no claude session is spawned.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from launcher import (
    MAX_FORKS_PER_RUN,
    SESSION_PREFIX,
    LaunchCommand,
    SessionLauncher,
    TmuxSession,
)

_CLAUDE = Path("/opt/bin/claude")


class TestLaunchCommand:
    """The pane command: claude binary + quoted initial prompt."""

    def test_prompt_is_shell_quoted(self) -> None:
        command = LaunchCommand(_CLAUDE, "do the thing; echo $HOME")
        shell = command.to_shell()
        assert shell.startswith("/opt/bin/claude ")
        assert "'do the thing; echo $HOME'" in shell

    def test_empty_prompt_is_refused(self) -> None:
        with pytest.raises(ValueError, match="empty prompt"):
            LaunchCommand(_CLAUDE, "")


class TestTmuxSessionArgv:
    """The detached-session invocation, with the isolated env injected."""

    def test_spawn_argv_is_detached_with_cwd_and_env(self, tmp_path: Path) -> None:
        session = TmuxSession("voxjuhw-t1")
        argv = session.spawn_argv(
            LaunchCommand(_CLAUDE, "hello"),
            tmp_path,
            {"CLAUDE_CONFIG_DIR": "/cfg"},
        )
        assert argv[:5] == ["tmux", "new-session", "-d", "-s", "voxjuhw-t1"]
        assert argv[5:7] == ["-c", str(tmp_path)]
        assert argv[7:9] == ["-e", "CLAUDE_CONFIG_DIR=/cfg"]
        assert argv[-1] == "/opt/bin/claude hello"


class TestSessionLauncherCap:
    """The mission's bounding rule: a hard fork cap per harness run."""

    def test_launch_rejects_unprefixed_names_before_forking(
        self, tmp_path: Path
    ) -> None:
        launcher = SessionLauncher(_CLAUDE)
        with pytest.raises(ValueError, match=SESSION_PREFIX):
            launcher.launch(
                "other-name",
                _FakeProject(tmp_path),  # type: ignore[arg-type]
                _FakeConfig(),  # type: ignore[arg-type]
                "prompt",
            )

    def test_cap_is_enforced(self, tmp_path: Path) -> None:
        launcher = SessionLauncher(_CLAUDE)
        spawned: list[str] = []
        project = _FakeProject(tmp_path)
        config = _FakeConfig()
        original = TmuxSession.spawn
        TmuxSession.spawn = (  # type: ignore[method-assign]
            lambda self, command, cwd, env: spawned.append(self.name)
        )
        try:
            for index in range(MAX_FORKS_PER_RUN):
                launcher.launch(
                    f"{SESSION_PREFIX}-{index}",
                    project,  # type: ignore[arg-type]
                    config,  # type: ignore[arg-type]
                    "prompt",
                )
            with pytest.raises(RuntimeError, match="fork cap"):
                launcher.launch(
                    f"{SESSION_PREFIX}-over",
                    project,  # type: ignore[arg-type]
                    config,  # type: ignore[arg-type]
                    "prompt",
                )
        finally:
            TmuxSession.spawn = original  # type: ignore[method-assign]
        assert len(spawned) == MAX_FORKS_PER_RUN


class _FakeProject:
    """Path-bearing stand-in for ScratchProject."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path


class _FakeConfig:
    """Env-bearing stand-in for IsolatedConfig."""

    def env(self) -> dict[str, str]:
        return {"CLAUDE_CONFIG_DIR": "/cfg"}
