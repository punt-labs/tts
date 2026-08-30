"""Fork command construction, session naming, and the per-run fork cap.

Everything here is pure argv/shape assertion -- no tmux session is created
and no claude session is spawned.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self

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

    def test_cap_is_enforced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        launcher = SessionLauncher(_CLAUDE)
        spawned: list[str] = []
        project = _FakeProject(tmp_path)
        config = _FakeConfig()
        monkeypatch.setattr(
            TmuxSession,
            "spawn",
            lambda self, command, cwd, env: spawned.append(self.name),
        )
        monkeypatch.setattr(TmuxSession, "alive", lambda self: True)
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
        assert len(spawned) == MAX_FORKS_PER_RUN

    def test_failed_spawn_does_not_consume_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A tmux failure must leave room for a retry: only a session that
        # verifiably exists counts against the per-run cap.
        launcher = SessionLauncher(_CLAUDE)
        project = _FakeProject(tmp_path)
        config = _FakeConfig()
        calls = {"n": 0}

        def _flaky_spawn(
            self: TmuxSession, command: object, cwd: object, env: object
        ) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                msg = "tmux exploded"
                raise RuntimeError(msg)

        monkeypatch.setattr(TmuxSession, "spawn", _flaky_spawn)
        monkeypatch.setattr(TmuxSession, "alive", lambda self: True)
        with pytest.raises(RuntimeError, match="tmux exploded"):
            launcher.launch(
                f"{SESSION_PREFIX}-fail",
                project,  # type: ignore[arg-type]
                config,  # type: ignore[arg-type]
                "prompt",
            )
        # Full budget still available after the failure.
        for index in range(MAX_FORKS_PER_RUN):
            launcher.launch(
                f"{SESSION_PREFIX}-retry-{index}",
                project,  # type: ignore[arg-type]
                config,  # type: ignore[arg-type]
                "prompt",
            )
        assert calls["n"] == 1 + MAX_FORKS_PER_RUN

    def test_fork_dead_at_startup_does_not_consume_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # tmux new-session exits 0 even when the pane command dies at
        # once (bad claude binary) and the session vanishes with it. The
        # launcher must verify liveness before spending budget.
        launcher = SessionLauncher(_CLAUDE)
        project = _FakeProject(tmp_path)
        config = _FakeConfig()
        monkeypatch.setattr(TmuxSession, "spawn", lambda self, command, cwd, env: None)
        monkeypatch.setattr(TmuxSession, "alive", lambda self: False)
        with pytest.raises(RuntimeError, match="died at startup"):
            launcher.launch(
                f"{SESSION_PREFIX}-doa",
                project,  # type: ignore[arg-type]
                config,  # type: ignore[arg-type]
                "prompt",
            )
        # Budget unconsumed: with a healthy session the full cap remains.
        monkeypatch.setattr(TmuxSession, "alive", lambda self: True)
        for index in range(MAX_FORKS_PER_RUN):
            launcher.launch(
                f"{SESSION_PREFIX}-ok-{index}",
                project,  # type: ignore[arg-type]
                config,  # type: ignore[arg-type]
                "prompt",
            )


class _FakeProject:
    """Path-bearing stand-in for ScratchProject."""

    _path: Path

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        self._path = path
        return self

    @property
    def path(self) -> Path:
        return self._path


class _FakeConfig:
    """Env-bearing stand-in for IsolatedConfig."""

    def __new__(cls) -> Self:
        return super().__new__(cls)

    def env(self) -> dict[str, str]:
        return {"CLAUDE_CONFIG_DIR": "/cfg"}
