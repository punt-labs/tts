"""Isolation guarantees of the scratch project and the isolated config dir."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from scratch import IsolatedConfig, ScratchProject

if TYPE_CHECKING:
    from pathlib import Path


class TestScratchProject:
    """A fresh git-initialized throwaway the fork works inside."""

    def test_create_materializes_git_repo_and_settings(self, tmp_path: Path) -> None:
        project = ScratchProject(tmp_path / "proj")
        project.create('{"permissions": {}}')
        assert (project.path / ".git").is_dir()
        assert (project.path / "README.md").exists()
        settings = json.loads((project.path / ".claude" / "settings.json").read_text())
        assert settings == {"permissions": {}}

    def test_remove_is_idempotent(self, tmp_path: Path) -> None:
        project = ScratchProject(tmp_path / "proj")
        project.create("{}")
        project.remove()
        assert not project.path.exists()
        project.remove()  # second call: no raise


class TestIsolatedConfig:
    """Fresh CLAUDE_CONFIG_DIR: seeded credentials + minimal state only."""

    def test_env_points_at_the_config_dir(self, tmp_path: Path) -> None:
        config = IsolatedConfig(tmp_path / "cfg")
        assert config.env()["CLAUDE_CONFIG_DIR"] == str(tmp_path / "cfg")

    def test_env_blanks_the_launchers_api_credentials(self, tmp_path: Path) -> None:
        env = IsolatedConfig(tmp_path / "cfg").env()
        assert env["ANTHROPIC_API_KEY"] == ""
        assert env["ANTHROPIC_AUTH_TOKEN"] == ""

    def test_credentials_are_copied_with_owner_only_mode(self, tmp_path: Path) -> None:
        source = tmp_path / "creds.json"
        source.write_text('{"oauth": "x"}', encoding="utf-8")
        config = IsolatedConfig(tmp_path / "cfg")
        config.create(tmp_path / "proj", source)
        copied = config.path / ".credentials.json"
        assert copied.read_text(encoding="utf-8") == '{"oauth": "x"}'
        assert copied.stat().st_mode & 0o777 == 0o600
        assert config.credentials_seeded is True

    def test_missing_credentials_source_is_tolerated_and_reported(
        self, tmp_path: Path
    ) -> None:
        config = IsolatedConfig(tmp_path / "cfg")
        config.create(tmp_path / "proj", tmp_path / "absent.json")
        assert not (config.path / ".credentials.json").exists()
        # The skip must be queryable so the runner can record it at seed
        # time instead of failing 240s later on an unexplained timeout.
        assert config.credentials_seeded is False

    def test_state_preaccepts_trust_for_exactly_the_project(
        self, tmp_path: Path
    ) -> None:
        config = IsolatedConfig(tmp_path / "cfg")
        project = tmp_path / "proj"
        config.create(project, tmp_path / "absent.json")
        state = json.loads((config.path / ".claude.json").read_text())
        assert state["hasCompletedOnboarding"] is True
        assert list(state["projects"]) == [str(project)]
        entry = state["projects"][str(project)]
        assert entry["hasTrustDialogAccepted"] is True
        assert entry["hasClaudeMdExternalIncludesApproved"] is False

    def test_remove_is_idempotent_and_takes_credentials_with_it(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "creds.json"
        source.write_text("{}", encoding="utf-8")
        config = IsolatedConfig(tmp_path / "cfg")
        config.create(tmp_path / "proj", source)
        config.remove()
        assert not config.path.exists()
        config.remove()  # second call: no raise
