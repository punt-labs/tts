"""Tests for punt_vox.doctor — DoctorCheck diagnostic checks."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.client_sync import VoxClientSync
from punt_vox.doctor import (
    CheckResult,
    DoctorCheck,
    format_results,
)
from punt_vox.types_health import HealthStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeVersionInfo(tuple[int, int, int]):
    """Tuple subclass that also exposes .major/.minor/.micro attributes."""

    def __new__(cls, major: int, minor: int, micro: int) -> _FakeVersionInfo:
        self = super().__new__(cls, (major, minor, micro))
        self.major = major  # type: ignore[attr-defined]
        self.minor = minor  # type: ignore[attr-defined]
        self.micro = micro  # type: ignore[attr-defined]
        return self


# ---------------------------------------------------------------------------
# CheckResult dataclass
# ---------------------------------------------------------------------------


class TestCheckResult:
    def test_frozen(self) -> None:
        r = CheckResult(name="test", passed=True, message="ok")
        with pytest.raises(AttributeError):
            r.name = "changed"  # type: ignore[misc]

    def test_defaults(self) -> None:
        r = CheckResult(name="test", passed=True, message="ok")
        assert r.detail == ""
        assert r.required is True
        assert r.symbol == "✓"
        assert r.status_kind == "pass"


# ---------------------------------------------------------------------------
# DoctorCheck construction
# ---------------------------------------------------------------------------


class TestDoctorCheckConstruction:
    def test_default_client_is_none(self) -> None:
        check = DoctorCheck()
        assert check._client is None

    def test_explicit_client(self) -> None:
        mock = MagicMock(spec=VoxClientSync)
        check = DoctorCheck(client=mock)
        assert check._client is mock


# ---------------------------------------------------------------------------
# check_python_version
# ---------------------------------------------------------------------------


class TestCheckPythonVersion:
    def test_current_python_passes(self) -> None:
        check = DoctorCheck()
        result = check.check_python_version()
        # We are running on 3.13+, so this should pass.
        assert result.passed is True
        v = sys.version_info
        assert f"{v.major}.{v.minor}" in result.message

    def test_old_python_fails(self) -> None:
        fake_vi = _FakeVersionInfo(3, 12, 0)
        with patch("punt_vox.doctor.sys") as mock_sys:
            mock_sys.version_info = fake_vi
            check = DoctorCheck()
            result = check.check_python_version()
        assert result.passed is False
        assert "3.13+" in result.message


# ---------------------------------------------------------------------------
# check_ffmpeg
# ---------------------------------------------------------------------------


class TestCheckFfmpeg:
    @patch("punt_vox.doctor.shutil.which", return_value="/usr/bin/ffmpeg")
    def test_ffmpeg_found_reports_present_no_path(self, _mock: MagicMock) -> None:
        result = DoctorCheck().check_ffmpeg()
        assert result.passed is True
        assert result.message == "ffmpeg: present"
        assert "/usr/bin/ffmpeg" not in result.message  # out-of-jail path dropped

    @patch("punt_vox.doctor.shutil.which", return_value=None)
    def test_ffmpeg_missing(self, _mock: MagicMock) -> None:
        result = DoctorCheck().check_ffmpeg()
        assert result.passed is False
        assert "not found" in result.message


# ---------------------------------------------------------------------------
# check_mpv (present + version gate)
# ---------------------------------------------------------------------------


class TestCheckMpv:
    @staticmethod
    def _proc(stdout: str) -> MagicMock:
        return MagicMock(stdout=stdout)

    @patch("punt_vox.doctor.shutil.which", return_value="/usr/bin/mpv")
    def test_present_recent_passes(self, _which: MagicMock) -> None:
        with patch(
            "punt_vox.doctor.subprocess.run",
            return_value=self._proc("mpv 0.38.0 Copyright"),
        ):
            result = DoctorCheck().check_mpv()
        assert result.passed is True
        assert result.message == "mpv: present (0.38.0)"
        assert "/usr/bin/mpv" not in result.message  # out-of-jail path dropped

    @patch("punt_vox.doctor.shutil.which", return_value=None)
    def test_missing_fails(self, _which: MagicMock) -> None:
        result = DoctorCheck().check_mpv()
        assert result.passed is False
        assert result.symbol == "✗"
        assert "not found" in result.message

    @patch("punt_vox.doctor.shutil.which", return_value="/usr/bin/mpv")
    def test_too_old_fails(self, _which: MagicMock) -> None:
        with patch(
            "punt_vox.doctor.subprocess.run",
            return_value=self._proc("mpv 0.30.0 Copyright"),
        ):
            result = DoctorCheck().check_mpv()
        assert result.passed is False
        assert result.symbol == "✗"
        assert "too old" in result.message
        assert "0.30.0" in result.message
        assert "0.35.0" in result.message  # names the required floor

    @patch("punt_vox.doctor.shutil.which", return_value="/usr/bin/mpv")
    def test_unparseable_version_fails(self, _which: MagicMock) -> None:
        with patch(
            "punt_vox.doctor.subprocess.run",
            return_value=self._proc("not a version line"),
        ):
            result = DoctorCheck().check_mpv()
        assert result.passed is False
        assert result.symbol == "✗"
        assert "unreadable" in result.message

    @patch("punt_vox.doctor.shutil.which", return_value="/usr/bin/mpv")
    def test_subprocess_error_fails(self, _which: MagicMock) -> None:
        with patch("punt_vox.doctor.subprocess.run", side_effect=OSError("boom")):
            result = DoctorCheck().check_mpv()
        assert result.passed is False
        assert "unreadable" in result.message


class TestParseMpvVersion:
    @pytest.mark.parametrize(
        ("output", "expected"),
        [
            ("mpv 0.38.0 Copyright © 2000-2024", (0, 38, 0)),
            ("mpv v0.35.1 Copyright", (0, 35, 1)),
            ("mpv 0.37 Copyright", (0, 37, 0)),
            ("mpv 0.40.0-git-abc123", (0, 40, 0)),
        ],
    )
    def test_parses_version(self, output: str, expected: tuple[int, int, int]) -> None:
        assert DoctorCheck._parse_mpv_version(output) == expected

    def test_no_version_returns_none(self) -> None:
        assert DoctorCheck._parse_mpv_version("no version here") is None


# ---------------------------------------------------------------------------
# check_daemon_health
# ---------------------------------------------------------------------------


class TestCheckDaemonHealth:
    def test_daemon_running_version_match(self) -> None:
        mock_client = MagicMock(spec=VoxClientSync)
        mock_client.health.return_value = HealthStatus(
            provider="elevenlabs", port=8421, daemon_version="5.0.0"
        )
        with patch("punt_vox.doctor.installed_version", return_value="5.0.0"):
            results = DoctorCheck(client=mock_client).check_daemon_health()
        assert len(results) == 1
        assert results[0].passed is True
        assert "8421" in results[0].message

    def test_daemon_running_version_mismatch(self) -> None:
        mock_client = MagicMock(spec=VoxClientSync)
        mock_client.health.return_value = HealthStatus(
            provider="elevenlabs", port=8421, daemon_version="4.8.0"
        )
        with patch("punt_vox.doctor.installed_version", return_value="5.0.0"):
            results = DoctorCheck(client=mock_client).check_daemon_health()
        assert len(results) == 1
        assert results[0].passed is False
        assert results[0].symbol == "⚠"
        assert "4.8.0" in results[0].message
        assert "5.0.0" in results[0].message

    def test_daemon_not_running(self) -> None:
        mock_client = MagicMock(spec=VoxClientSync)
        mock_client.health.side_effect = VoxdConnectionError("refused")
        results = DoctorCheck(client=mock_client).check_daemon_health()
        assert len(results) == 1
        assert results[0].passed is False
        assert "not running" in results[0].message

    def test_daemon_unhealthy(self) -> None:
        mock_client = MagicMock(spec=VoxClientSync)
        mock_client.health.side_effect = VoxdProtocolError("bad state")
        results = DoctorCheck(client=mock_client).check_daemon_health()
        assert len(results) == 1
        assert results[0].passed is False
        assert "unhealthy" in results[0].message


# ---------------------------------------------------------------------------
# check_env_overrides
# ---------------------------------------------------------------------------


class TestCheckEnvOverrides:
    def test_no_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VOXD_HOST", raising=False)
        monkeypatch.delenv("VOXD_PORT", raising=False)
        monkeypatch.delenv("VOXD_TOKEN", raising=False)
        results = DoctorCheck().check_env_overrides()
        assert results == []

    def test_host_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VOXD_HOST", "192.168.1.50")
        monkeypatch.delenv("VOXD_PORT", raising=False)
        monkeypatch.delenv("VOXD_TOKEN", raising=False)
        results = DoctorCheck().check_env_overrides()
        assert len(results) == 1
        assert results[0].passed is True
        assert "192.168.1.50" in results[0].message

    def test_token_masked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VOXD_HOST", raising=False)
        monkeypatch.delenv("VOXD_PORT", raising=False)
        monkeypatch.setenv("VOXD_TOKEN", "secret-token-123")
        results = DoctorCheck().check_env_overrides()
        assert len(results) == 1
        assert "***" in results[0].message
        assert "secret-token-123" not in results[0].message


# ---------------------------------------------------------------------------
# check_uvx
# ---------------------------------------------------------------------------


class TestCheckUvx:
    @patch("punt_vox.doctor.shutil.which", return_value="/usr/local/bin/uvx")
    def test_uvx_found(self, _mock: MagicMock) -> None:
        result = DoctorCheck().check_uvx()
        assert result.passed is True
        assert result.required is False

    @patch("punt_vox.doctor.shutil.which", return_value=None)
    def test_uvx_missing(self, _mock: MagicMock) -> None:
        result = DoctorCheck().check_uvx()
        assert result.passed is False
        assert result.required is False
        assert result.status_kind == "skip"


# ---------------------------------------------------------------------------
# check_output_dir
# ---------------------------------------------------------------------------


class TestCheckOutputDir:
    def test_writable_dir_reports_verdict_no_path(self, tmp_path: Path) -> None:
        with patch("punt_vox.doctor.default_output_dir", return_value=tmp_path):
            results = DoctorCheck().check_output_dir()
        assert len(results) == 1
        assert results[0].passed is True
        assert results[0].message == "output: writable"
        assert str(tmp_path) not in results[0].message  # no absolute path

    def test_missing_dir(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent"
        with patch("punt_vox.doctor.default_output_dir", return_value=missing):
            results = DoctorCheck().check_output_dir()
        assert len(results) == 1
        assert results[0].passed is False
        assert results[0].symbol == "⚠"
        assert "absent" in results[0].message
        assert str(missing) not in results[0].message  # no absolute path


# ---------------------------------------------------------------------------
# check_claude_desktop
# ---------------------------------------------------------------------------


class TestCheckClaudeDesktop:
    def test_config_not_found(self, tmp_path: Path) -> None:
        fake_path = tmp_path / "nonexistent" / "config.json"
        with patch(
            "punt_vox.doctor.claude_desktop_config_path",
            return_value=fake_path,
        ):
            results = DoctorCheck().check_claude_desktop()
        assert len(results) == 2
        assert all(not r.required for r in results)

    def test_config_with_vox_registered(self, tmp_path: Path) -> None:
        config = tmp_path / "config.json"
        config.write_text(
            json.dumps({"mcpServers": {"vox": {"command": "uvx"}}}),
            encoding="utf-8",
        )
        with patch(
            "punt_vox.doctor.claude_desktop_config_path",
            return_value=config,
        ):
            results = DoctorCheck().check_claude_desktop()
        assert len(results) == 2
        assert results[1].passed is True
        assert "registered" in results[1].message

    def test_config_without_vox(self, tmp_path: Path) -> None:
        config = tmp_path / "config.json"
        config.write_text(
            json.dumps({"mcpServers": {}}),
            encoding="utf-8",
        )
        with patch(
            "punt_vox.doctor.claude_desktop_config_path",
            return_value=config,
        ):
            results = DoctorCheck().check_claude_desktop()
        assert len(results) == 2
        assert results[1].passed is False
        assert "not registered" in results[1].message


# ---------------------------------------------------------------------------
# format_results
# ---------------------------------------------------------------------------


class TestFormatResults:
    def test_all_pass(self) -> None:
        results = [
            CheckResult(name="a", passed=True, message="a ok"),
            CheckResult(name="b", passed=True, message="b ok"),
        ]
        payload, text = format_results(results)
        assert payload["passed"] == 2
        assert payload["failed"] == 0
        assert "2 passed, 0 failed" in text

    def test_one_fail(self) -> None:
        results = [
            CheckResult(
                name="bad",
                passed=False,
                message="bad",
                symbol="✗",
                status_kind="fail",
            ),
        ]
        payload, _text = format_results(results)
        assert payload["failed"] == 1

    def test_warnings_counted(self) -> None:
        results = [
            CheckResult(
                name="warn",
                passed=False,
                message="warning msg",
                symbol="⚠",
                status_kind="warn",
            ),
        ]
        payload, text = format_results(results)
        assert payload["warned"] == 1
        assert "1 warning" in text


# ---------------------------------------------------------------------------
# run_all integration
# ---------------------------------------------------------------------------


class TestRunAll:
    def test_returns_list_of_check_results(self) -> None:
        mock_client = MagicMock(spec=VoxClientSync)
        mock_client.health.side_effect = VoxdConnectionError("nope")
        with patch("punt_vox.doctor.installed_version", return_value="5.0.0"):
            results = DoctorCheck(client=mock_client).run_all()
        assert isinstance(results, list)
        assert all(isinstance(r, CheckResult) for r in results)
        assert len(results) >= 4  # python, ffmpeg, daemon, uvx at minimum

    def test_no_which_or_config_path_leaks_into_any_message(
        self, tmp_path: Path
    ) -> None:
        """No out-of-jail host path (a `which` result, a config path) reaches a
        message -- the a7dd chroot: doctor emits verdicts, never host locations."""
        mock_client = MagicMock(spec=VoxClientSync)
        mock_client.health.side_effect = VoxdConnectionError("nope")
        which_path = "/opt/host/bin/tool"
        config_path = tmp_path / "sekret-home" / "config.json"
        with (
            patch("punt_vox.doctor.shutil.which", return_value=which_path),
            patch("punt_vox.doctor.default_output_dir", return_value=tmp_path),
            patch(
                "punt_vox.doctor.claude_desktop_config_path", return_value=config_path
            ),
        ):
            results = DoctorCheck(client=mock_client).run_all()
        for r in results:
            assert which_path not in r.message  # no binary install path
            assert str(config_path) not in r.message  # no absolute config path
