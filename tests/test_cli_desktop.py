"""Tests for the ``vox desktop install`` / ``vox desktop uninstall`` verbs.

The platform question is the subject of most of these: the CLI must take its
verdict from :class:`DesktopInstaller` rather than probing ``platform.system``
itself, so a Linux host registers at the XDG location and an unverified host
refuses instead of writing a file Claude Desktop will never read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from punt_vox.__main__ import app

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

    @pytest.mark.parametrize("value", ["vox", ["vox"]])
    def test_non_object_mcpservers_is_rejected(
        self, value: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-object ``mcpServers`` crashed the merge instead of reporting.

        The string case is the nastier of the two: ``"vox" in "vox"`` is a
        substring test that reports an overwrite, and the item assignment
        after it raises ``TypeError`` out of the CLI.
        """
        config_path = _linux_host(monkeypatch, tmp_path)
        config_path.parent.mkdir(parents=True)
        original = json.dumps({"mcpServers": value})
        config_path.write_text(original, encoding="utf-8")

        result = _install(out=tmp_path / "audio")

        assert result.exit_code == 1
        assert 'must have a JSON object under "mcpServers"' in result.output
        assert config_path.read_text(encoding="utf-8") == original

    def test_non_utf8_config_is_a_clean_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Undecodable bytes fail in ``read_text``, not in ``json.loads``."""
        config_path = _linux_host(monkeypatch, tmp_path)
        config_path.parent.mkdir(parents=True)
        config_path.write_bytes(b'{"mcpServers": {"caf\xe9": {}}}')

        result = _install(out=tmp_path / "audio")

        assert result.exit_code == 1
        assert "Could not read" in result.output


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
        # This refusal is the last of the four, so it is the one that proves
        # the ordering: it used to fire *after* the output directory had
        # already been created.
        assert not (tmp_path / "audio").exists()
        assert not (tmp_path / "xdg").exists()

    @pytest.mark.parametrize(
        "cause", ["missing_uvx", "unsupported_platform", "no_credentials"]
    )
    def test_no_refusal_creates_anything_on_disk(
        self, cause: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every way ``install`` can decline leaves the filesystem untouched.

        The invariant, not one instance of it. ``install`` resolves four
        prerequisites and each can exit non-zero; the ordering bug this guards
        was invisible because the two refusals under test at the time both
        fired before the ``mkdir``, while the credentials refusal fired after
        it. Parametrising over the causes means a fifth refusal added above
        the ``mkdir`` boundary later cannot reintroduce it silently.
        """
        _on_platform(monkeypatch, "Linux")
        _with_uvx(monkeypatch, "/usr/bin/uvx")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

        if cause == "missing_uvx":
            _with_uvx(monkeypatch, None)
        elif cause == "unsupported_platform":
            _on_platform(monkeypatch, "Windows")
        else:

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
        assert not (tmp_path / "audio").exists()
        assert not (tmp_path / "xdg").exists()


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


class TestConfigWriteIsAtomicAndPrivate:
    """``claude_desktop_config.json`` is shared and holds other servers' secrets.

    vox rewrites the whole document to change one key of it, so the write has
    to be a rename over a fully-written temp file rather than a truncate --
    a partial write loses every other MCP server's entry, not just vox's.
    """

    def test_created_config_is_owner_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = _linux_host(monkeypatch, tmp_path)

        assert _install(out=tmp_path / "audio").exit_code == 0

        assert config_path.stat().st_mode & 0o777 == 0o600

    def test_created_config_directory_is_owner_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """vox creates ``Claude/`` when the app has not run on this host yet."""
        config_path = _linux_host(monkeypatch, tmp_path)
        assert not config_path.parent.exists()

        assert _install(out=tmp_path / "audio").exit_code == 0

        assert config_path.parent.stat().st_mode & 0o777 == 0o700

    def test_rewrite_narrows_a_world_readable_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An existing 0644 config comes out 0600, matching the Desktop app."""
        config_path = _linux_host(monkeypatch, tmp_path)
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        config_path.chmod(0o644)

        assert _install(out=tmp_path / "audio").exit_code == 0

        assert config_path.stat().st_mode & 0o777 == 0o600

    def test_successful_write_leaves_no_temp_sibling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = _linux_host(monkeypatch, tmp_path)

        assert _install(out=tmp_path / "audio").exit_code == 0

        assert sorted(p.name for p in config_path.parent.iterdir()) == [
            config_path.name
        ]

    def test_failed_write_leaves_the_old_config_and_no_temp_sibling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A write that dies mid-stream must not damage the shared document.

        This is the whole point of the rename: the previous truncate-then-write
        would have left an empty or half-written file here, destroying the
        ``other`` entry that vox does not own. The failure is also reported the
        way a failed *read* already is -- one error line, not a traceback.
        """
        config_path = _linux_host(monkeypatch, tmp_path)
        config_path.parent.mkdir(parents=True)
        original = json.dumps({"mcpServers": {"other": {"command": "x"}}})
        config_path.write_text(original, encoding="utf-8")

        def _boom(_self: object, _path: Path, _text: str) -> None:
            raise OSError("simulated disk full")

        monkeypatch.setattr(f"{_CLI_MOD}.DesktopCli._replace_atomically", _boom)

        result = _install(out=tmp_path / "audio")

        assert result.exit_code == 1
        assert "Could not write" in result.output
        assert config_path.read_text(encoding="utf-8") == original
        assert sorted(p.name for p in config_path.parent.iterdir()) == [
            config_path.name
        ]

    def test_a_symlinked_config_is_written_through_not_replaced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dotfiles-managed config is a symlink; the rename must not eat it.

        Renaming onto the link replaces it with a regular file and leaves the
        real target holding the old registration -- which chezmoi or stow then
        restores over vox's write on the next apply.
        """
        config_path = _linux_host(monkeypatch, tmp_path)
        real = tmp_path / "dotfiles" / "claude_desktop_config.json"
        real.parent.mkdir(parents=True)
        real.write_text(
            json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8"
        )
        config_path.parent.mkdir(parents=True)
        config_path.symlink_to(real)

        assert _install(out=tmp_path / "audio").exit_code == 0

        assert config_path.is_symlink()
        assert config_path.readlink() == real
        installed = json.loads(real.read_text(encoding="utf-8"))
        assert installed["mcpServers"]["vox"]["args"] == [
            "--from",
            "punt-vox",
            "vox",
            "mcp",
        ]
        assert "other" in installed["mcpServers"]
        assert sorted(p.name for p in real.parent.iterdir()) == [real.name]

        assert CliRunner().invoke(app, ["desktop", "uninstall"]).exit_code == 0

        assert config_path.is_symlink()
        assert "vox" not in json.loads(real.read_text(encoding="utf-8"))["mcpServers"]

    def test_a_dying_write_removes_its_own_temp_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cleanup is on the atomic swap itself, not only on its caller."""
        config_path = _linux_host(monkeypatch, tmp_path)
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

        def _boom(_self: Path, _mode: int) -> None:
            raise OSError("simulated chmod failure")

        monkeypatch.setattr(f"{_CLI_MOD}.Path.chmod", _boom)

        result = _install(out=tmp_path / "audio")

        assert result.exit_code == 1
        assert sorted(p.name for p in config_path.parent.iterdir()) == [
            config_path.name
        ]
