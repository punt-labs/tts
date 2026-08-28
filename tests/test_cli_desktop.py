"""Tests for the ``vox desktop install`` / ``vox desktop uninstall`` verbs.

The platform question is the subject of most of these: the CLI must take its
verdict from :class:`DesktopInstaller` rather than probing ``platform.system``
itself, so a Linux host registers at the XDG location and an unverified host
refuses instead of writing a file Claude Desktop will never read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from typer.testing import CliRunner, Result

from punt_vox.__main__ import app

if TYPE_CHECKING:
    import pytest

_INSTALL_MOD = "punt_vox.desktop_install"
_CLI_MOD = "punt_vox.cli_desktop"


def _on_platform(monkeypatch: pytest.MonkeyPatch, system: str) -> None:
    """Pin the platform the installer resolves its config location from."""
    monkeypatch.setattr(f"{_INSTALL_MOD}.platform.system", lambda: system)


def _with_uvx(monkeypatch: pytest.MonkeyPatch, uvx: str | None) -> None:
    """Pin what ``shutil.which('uvx')`` reports to the CLI."""

    def _which(_name: str) -> str | None:
        return uvx

    monkeypatch.setattr(f"{_CLI_MOD}.shutil.which", _which)


def _linux_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Stage a Linux host with a private XDG root; return the config path."""
    _on_platform(monkeypatch, "Linux")
    _with_uvx(monkeypatch, "/usr/bin/uvx")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "Claude" / "claude_desktop_config.json"


def _install(*extra: str, out: Path) -> Result:
    return CliRunner().invoke(
        app,
        ["desktop", "install", "--provider", "say", "--output-dir", str(out), *extra],
    )


class TestInstallPlatformDispatch:
    def test_linux_registers_at_the_xdg_location(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = _linux_host(monkeypatch, tmp_path)

        result = CliRunner().invoke(
            app,
            [
                "desktop",
                "install",
                "--provider",
                "say",
                "--output-dir",
                str(tmp_path / "audio"),
            ],
        )

        assert result.exit_code == 0
        data = json.loads(config_path.read_text(encoding="utf-8"))
        entry = data["mcpServers"]["vox"]
        assert entry["command"] == "/usr/bin/uvx"
        assert entry["args"] == ["--from", "punt-vox", "vox", "mcp"]
        assert entry["env"]["TTS_PROVIDER"] == "say"

    def test_unsupported_platform_refuses_and_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The old behaviour warned and wrote a macOS path anyway."""
        _on_platform(monkeypatch, "Windows")
        _with_uvx(monkeypatch, "/usr/bin/uvx")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        audio_dir = tmp_path / "audio"

        result = _install(out=audio_dir)

        assert result.exit_code == 1
        assert "Windows" in result.output
        assert not (tmp_path / "xdg").exists()
        # The refusal lands before any directory is created on disk.
        assert not audio_dir.exists()

    def test_unsupported_platform_json_gets_an_error_envelope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _on_platform(monkeypatch, "Windows")
        _with_uvx(monkeypatch, "/usr/bin/uvx")

        result = _install("--json", out=tmp_path / "audio")

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert "Unsupported platform" in payload["error"]

    def test_uninstall_refuses_on_unsupported_platform(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _on_platform(monkeypatch, "Windows")

        result = CliRunner().invoke(app, ["desktop", "uninstall"])

        assert result.exit_code == 1
        assert "Unsupported platform" in result.output


class TestInstallConfigMerge:
    def test_existing_entry_is_overwritten_and_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = _linux_host(monkeypatch, tmp_path)
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps({"mcpServers": {"vox": {"command": "stale"}}}),
            encoding="utf-8",
        )

        result = _install("--json", out=tmp_path / "audio")

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["overwritten"] is True
        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert data["mcpServers"]["vox"]["command"] == "/usr/bin/uvx"

    def test_other_servers_and_keys_survive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = _linux_host(monkeypatch, tmp_path)
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}),
            encoding="utf-8",
        )

        assert _install(out=tmp_path / "audio").exit_code == 0

        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert data["theme"] == "dark"
        assert data["mcpServers"]["other"] == {"command": "x"}
        assert "vox" in data["mcpServers"]

    def test_malformed_config_is_a_clean_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = _linux_host(monkeypatch, tmp_path)
        config_path.parent.mkdir(parents=True)
        config_path.write_text("{not json", encoding="utf-8")

        result = _install(out=tmp_path / "audio")

        assert result.exit_code == 1
        assert "Could not read" in result.output

    def test_non_object_config_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A JSON array would crash the ``setdefault`` merge."""
        config_path = _linux_host(monkeypatch, tmp_path)
        config_path.parent.mkdir(parents=True)
        config_path.write_text("[]", encoding="utf-8")

        result = _install(out=tmp_path / "audio")

        assert result.exit_code == 1
        assert "must be a JSON object" in result.output


class TestInstallPrerequisites:
    def test_missing_uvx_names_the_fix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _on_platform(monkeypatch, "Linux")
        _with_uvx(monkeypatch, None)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

        result = _install(out=tmp_path / "audio")

        assert result.exit_code == 1
        assert "uvx not found" in result.output
        assert not (tmp_path / "xdg").exists()

    def test_explicit_uvx_path_wins_over_lookup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = _linux_host(monkeypatch, tmp_path)
        _with_uvx(monkeypatch, None)

        result = _install("--uvx-path", "/opt/uvx", out=tmp_path / "audio")

        assert result.exit_code == 0
        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert data["mcpServers"]["vox"]["command"] == "/opt/uvx"

    def test_unknown_provider_credentials_are_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--provider`` omitted with no credentials in view: nothing to write."""
        _linux_host(monkeypatch, tmp_path)
        for key in ("TTS_PROVIDER", "ELEVENLABS_API_KEY", "OPENAI_API_KEY"):
            monkeypatch.delenv(key, raising=False)

        def _nothing_ready(_self: object) -> str | None:
            return None

        monkeypatch.setattr(
            f"{_INSTALL_MOD}.ProviderCredentials.preferred", _nothing_ready
        )

        result = CliRunner().invoke(
            app,
            ["desktop", "install", "--output-dir", str(tmp_path / "audio")],
        )

        assert result.exit_code == 1
        assert "No TTS provider credentials" in result.output


class TestUninstall:
    def test_absent_config_is_a_no_op(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _linux_host(monkeypatch, tmp_path)

        result = CliRunner().invoke(app, ["desktop", "uninstall"])

        assert result.exit_code == 0
        assert "nothing to do" in result.output

    def test_absent_vox_entry_is_a_no_op(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = _linux_host(monkeypatch, tmp_path)
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8"
        )

        result = CliRunner().invoke(app, ["desktop", "uninstall"])

        assert result.exit_code == 0
        assert "not registered" in result.output
        assert "other" in config_path.read_text(encoding="utf-8")

    def test_registered_entry_is_removed_leaving_siblings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = _linux_host(monkeypatch, tmp_path)
        assert _install(out=tmp_path / "audio").exit_code == 0
        data = json.loads(config_path.read_text(encoding="utf-8"))
        data["mcpServers"]["other"] = {"command": "x"}
        config_path.write_text(json.dumps(data), encoding="utf-8")

        result = CliRunner().invoke(app, ["desktop", "uninstall"])

        assert result.exit_code == 0
        remaining = json.loads(config_path.read_text(encoding="utf-8"))
        assert "vox" not in remaining["mcpServers"]
        assert "other" in remaining["mcpServers"]
