"""Pins for the fork's isolation plumbing (the subprocess-free parts).

The launch chain itself is exercised live by the harness; these pins cover
the pieces whose failure would silently skew the measurement: the relay
assets landing in the CONFIG dir (not the project, where the fork's file
tools would trip over them), the environment blanking the launcher's own
credentials, and the missing-credentials path being visible instead of a
late hooks timeout.
"""

from __future__ import annotations

import json
from pathlib import Path

from scratch import IsolatedConfig


class TestDepositRelay:
    """Relay assets live in the config dir and are runnable."""

    def test_deposits_script_stamper_and_counter_dir(self, tmp_path: Path) -> None:
        config = IsolatedConfig(tmp_path / "cfg")
        config.deposit_relay("#!/bin/sh\necho hi\n")
        assert config.relay_script.read_text(encoding="utf-8").startswith("#!/bin/sh")
        assert config.stamper_script.exists()
        assert config.counter_dir.is_dir()

    def test_relay_script_is_executable(self, tmp_path: Path) -> None:
        config = IsolatedConfig(tmp_path / "cfg")
        config.deposit_relay("#!/bin/sh\n")
        assert config.relay_script.stat().st_mode & 0o111

    def test_stamper_copy_matches_the_spike_source(self, tmp_path: Path) -> None:
        # The fork runs the COPY under the system python3; a stale or
        # truncated copy would stamp nothing and every latency/gap number
        # would silently be None.
        config = IsolatedConfig(tmp_path / "cfg")
        config.deposit_relay("#!/bin/sh\n")
        source = (Path(__file__).parent / "relay_stamp.py").read_bytes()
        assert config.stamper_script.read_bytes() == source

    def test_relay_assets_are_inside_the_config_dir(self, tmp_path: Path) -> None:
        config = IsolatedConfig(tmp_path / "cfg")
        for asset in (config.relay_script, config.stamper_script, config.counter_dir):
            assert asset.is_relative_to(tmp_path / "cfg")


class TestEnvIsolation:
    """The fork sees the fresh config dir and no inherited API credentials."""

    def test_env_points_at_the_config_dir_and_blanks_keys(self, tmp_path: Path) -> None:
        env = IsolatedConfig(tmp_path / "cfg").env()
        assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "cfg")
        assert env["ANTHROPIC_API_KEY"] == ""
        assert env["ANTHROPIC_AUTH_TOKEN"] == ""


class TestCredentialSeeding:
    """Absent credentials are recorded at seed time, not discovered later."""

    def test_missing_credentials_source_is_a_visible_skip(self, tmp_path: Path) -> None:
        config = IsolatedConfig(tmp_path / "cfg")
        config.create(tmp_path / "project", tmp_path / "no-such-credentials.json")
        assert not config.credentials_seeded
        assert not (config.path / ".credentials.json").exists()

    def test_present_credentials_are_copied_with_tight_mode(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "creds.json"
        source.write_text('{"oauth": "x"}', encoding="utf-8")
        config = IsolatedConfig(tmp_path / "cfg")
        config.create(tmp_path / "project", source)
        assert config.credentials_seeded
        copied = config.path / ".credentials.json"
        assert copied.stat().st_mode & 0o777 == 0o600

    def test_state_pre_trusts_exactly_the_scratch_project(self, tmp_path: Path) -> None:
        config = IsolatedConfig(tmp_path / "cfg")
        project = tmp_path / "project"
        config.create(project, tmp_path / "absent.json")
        state = json.loads((config.path / ".claude.json").read_text(encoding="utf-8"))
        assert state["hasCompletedOnboarding"] is True
        assert set(state["projects"]) == {str(project)}
        assert state["projects"][str(project)]["hasTrustDialogAccepted"] is True
