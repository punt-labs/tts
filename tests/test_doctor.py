"""Tests for punt_vox.doctor — DoctorCheck diagnostic checks."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.client_sync import VoxClientSync
from punt_vox.doctor import DoctorCheck
from punt_vox.doctor_result import CheckResult, CheckResults
from punt_vox.types_health import HealthStatus
from punt_vox.types_provider import ProviderReadiness, ProviderStatusPayload

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

    @patch("punt_vox.doctor_mpv.shutil.which", return_value="/usr/bin/mpv")
    def test_present_recent_passes(self, _which: MagicMock) -> None:
        with patch(
            "punt_vox.doctor_mpv.subprocess.run",
            return_value=self._proc("mpv 0.38.0 Copyright"),
        ):
            result = DoctorCheck().check_mpv()
        assert result.passed is True
        assert result.message == "mpv: present (0.38.0)"
        assert "/usr/bin/mpv" not in result.message  # out-of-jail path dropped

    @patch("punt_vox.doctor_mpv.shutil.which", return_value=None)
    def test_missing_fails(self, _which: MagicMock) -> None:
        result = DoctorCheck().check_mpv()
        assert result.passed is False
        assert result.symbol == "✗"
        assert "not found" in result.message

    @patch("punt_vox.doctor_mpv.shutil.which", return_value="/usr/bin/mpv")
    def test_too_old_fails(self, _which: MagicMock) -> None:
        with patch(
            "punt_vox.doctor_mpv.subprocess.run",
            return_value=self._proc("mpv 0.30.0 Copyright"),
        ):
            result = DoctorCheck().check_mpv()
        assert result.passed is False
        assert result.symbol == "✗"
        assert "too old" in result.message
        assert "0.30.0" in result.message
        assert "0.35.0" in result.message  # names the required floor

    @patch("punt_vox.doctor_mpv.shutil.which", return_value="/usr/bin/mpv")
    def test_unparseable_version_fails(self, _which: MagicMock) -> None:
        with patch(
            "punt_vox.doctor_mpv.subprocess.run",
            return_value=self._proc("not a version line"),
        ):
            result = DoctorCheck().check_mpv()
        assert result.passed is False
        assert result.symbol == "✗"
        assert "unreadable" in result.message

    @patch("punt_vox.doctor_mpv.shutil.which", return_value="/usr/bin/mpv")
    def test_subprocess_error_fails(self, _which: MagicMock) -> None:
        with patch("punt_vox.doctor_mpv.subprocess.run", side_effect=OSError("boom")):
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
        from punt_vox.doctor_mpv import MpvCheck

        assert MpvCheck.parse_version(output) == expected

    def test_no_version_returns_none(self) -> None:
        from punt_vox.doctor_mpv import MpvCheck

        assert MpvCheck.parse_version("no version here") is None


# ---------------------------------------------------------------------------
# check_daemon_health
# ---------------------------------------------------------------------------


class TestCheckDaemonHealth:
    def test_daemon_running_version_match(self) -> None:
        mock_client = MagicMock(spec=VoxClientSync)
        mock_client.health.return_value = HealthStatus(
            port=8421, daemon_version="5.0.0"
        )
        with patch("punt_vox.doctor.installed_version", return_value="5.0.0"):
            results = DoctorCheck(client=mock_client).check_daemon_health()
        assert len(results) == 1
        assert results[0].passed is True
        assert "8421" in results[0].message
        # ``provider`` is deliberately absent from the health line (design
        # §3.6 / D4); per-provider readiness moves to PR 3's status block.
        assert "provider:" not in results[0].message

    def test_daemon_running_version_mismatch(self) -> None:
        mock_client = MagicMock(spec=VoxClientSync)
        mock_client.health.return_value = HealthStatus(
            port=8421, daemon_version="4.8.0"
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
# check_provider_readiness
# ---------------------------------------------------------------------------


class TestCheckProviderReadiness:
    """The pointer target of every F2 error message; the daemon answers here.

    ``vox doctor`` must not read the caller's environment (design D1
    correction, §3.5); every case exercises the daemon-authoritative
    payload rather than a local probe.
    """

    def _payload(
        self,
        rows: tuple[ProviderReadiness, ...],
        preferred: str | None,
    ) -> ProviderStatusPayload:
        return ProviderStatusPayload(rows, preferred=preferred)

    def test_ready_provider_is_a_pass(self) -> None:
        mock_client = MagicMock(spec=VoxClientSync)
        mock_client.provider_status.return_value = self._payload(
            (ProviderReadiness(name="openai", ready=True, reason="ok", detail=""),),
            preferred="openai",
        )
        results = DoctorCheck(client=mock_client).check_provider_readiness()
        # Head line names the preferred provider; per-provider walk follows.
        assert "preferred → openai" in results[0].message
        assert results[0].passed is True
        assert any("openai: ready" in r.message for r in results[1:])

    def test_unready_provider_is_a_warn_not_a_fail(self) -> None:
        # A single-provider host is a normal configuration -- only the
        # state-declared provider being unavailable is a hard failure,
        # and that surfaces at synthesize time.
        mock_client = MagicMock(spec=VoxClientSync)
        mock_client.provider_status.return_value = self._payload(
            (
                ProviderReadiness(
                    name="polly",
                    ready=False,
                    reason="no_credentials",
                    detail="voxd has no AWS credentials",
                ),
            ),
            preferred=None,
        )
        results = DoctorCheck(client=mock_client).check_provider_readiness()
        polly_line = next(r for r in results if "polly" in r.message)
        assert polly_line.required is False
        assert polly_line.status_kind == "skip"
        assert "voxd has no AWS credentials" in polly_line.message

    def test_no_preferred_is_a_hard_fail(self) -> None:
        mock_client = MagicMock(spec=VoxClientSync)
        mock_client.provider_status.return_value = self._payload((), preferred=None)
        results = DoctorCheck(client=mock_client).check_provider_readiness()
        assert results[0].passed is False
        assert "no provider is usable" in results[0].message

    def test_daemon_down_reports_optional(self) -> None:
        # ``check_daemon_health`` already reports the down state as a hard
        # fail; this section reports informationally so the reader knows
        # why the walk was skipped.
        mock_client = MagicMock(spec=VoxClientSync)
        mock_client.provider_status.side_effect = VoxdConnectionError("refused")
        results = DoctorCheck(client=mock_client).check_provider_readiness()
        assert len(results) == 1
        assert results[0].required is False
        assert results[0].symbol == "○"

    def test_protocol_error_is_a_warn(self) -> None:
        mock_client = MagicMock(spec=VoxClientSync)
        mock_client.provider_status.side_effect = VoxdProtocolError("bad frame")
        results = DoctorCheck(client=mock_client).check_provider_readiness()
        assert len(results) == 1
        assert results[0].symbol == "⚠"
        assert "unavailable" in results[0].message


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
# check_deposited_guide
# ---------------------------------------------------------------------------


class TestCheckDepositedGuide:
    """The staleness check compares the deposited guide's source-hash stamp
    to a fresh hash of the packaged asset. The four verdicts are distinct
    on purpose -- absent stamp must not read as a false pass, and being
    outside a repo (or in a repo with vox not enabled) is not applicable,
    not a failure. See ``docs/bd/vox-prfr`` for the rot this guards.
    """

    def _make_repo(self, tmp_path: Path) -> Path:
        (tmp_path / ".git").mkdir()
        (tmp_path / ".punt-labs" / "vox").mkdir(parents=True)
        return tmp_path

    def _packaged(self, tmp_path: Path) -> Path:
        asset = tmp_path / "packaged.md"
        asset.write_text("packaged body\n", encoding="utf-8")
        return asset

    def test_no_repo_is_not_applicable(self, tmp_path: Path) -> None:
        with patch("punt_vox.doctor.find_repo_root", return_value=None):
            results = DoctorCheck().check_deposited_guide()
        assert results == []

    def test_repo_without_deposited_guide_is_not_applicable(
        self, tmp_path: Path
    ) -> None:
        repo = self._make_repo(tmp_path)
        with patch("punt_vox.doctor.find_repo_root", return_value=repo):
            results = DoctorCheck().check_deposited_guide()
        # vox not enabled here -- no deposited guide -- reported as N/A.
        assert results == []

    def test_agree_passes(self, tmp_path: Path) -> None:
        from punt_vox.guide_stamp import GuideStamp

        repo = self._make_repo(tmp_path)
        packaged = self._packaged(tmp_path)
        stamp = GuideStamp(packaged)
        deposited = repo / ".punt-labs" / "vox" / "CLAUDE.md"
        deposited.write_text(stamp.stamped("packaged body\n"), encoding="utf-8")

        with (
            patch("punt_vox.doctor.find_repo_root", return_value=repo),
            patch.object(GuideStamp, "for_packaged_asset", return_value=stamp),
        ):
            results = DoctorCheck().check_deposited_guide()
        assert len(results) == 1
        assert results[0].passed is True
        assert "up to date" in results[0].message

    def test_diverge_fails(self, tmp_path: Path) -> None:
        from punt_vox.guide_stamp import GuideStamp

        repo = self._make_repo(tmp_path)
        packaged = self._packaged(tmp_path)
        stamp = GuideStamp(packaged)
        deposited = repo / ".punt-labs" / "vox" / "CLAUDE.md"
        deposited.write_text(stamp.stamped("packaged body\n"), encoding="utf-8")
        # Packaged asset drifts after the deposit was stamped.
        packaged.write_text("new packaged body\n", encoding="utf-8")

        with (
            patch("punt_vox.doctor.find_repo_root", return_value=repo),
            patch.object(GuideStamp, "for_packaged_asset", return_value=stamp),
        ):
            results = DoctorCheck().check_deposited_guide()
        assert len(results) == 1
        assert results[0].passed is False
        assert results[0].symbol == "✗"
        assert "out of date" in results[0].message
        assert "vox enable" in results[0].message

    def test_absent_stamp_warns_and_does_not_pass(self, tmp_path: Path) -> None:
        from punt_vox.guide_stamp import GuideStamp

        repo = self._make_repo(tmp_path)
        packaged = self._packaged(tmp_path)
        stamp = GuideStamp(packaged)
        deposited = repo / ".punt-labs" / "vox" / "CLAUDE.md"
        # A guide deposited before this stamping existed -- no HTML-comment tail.
        deposited.write_text("unstamped body\n", encoding="utf-8")

        with (
            patch("punt_vox.doctor.find_repo_root", return_value=repo),
            patch.object(GuideStamp, "for_packaged_asset", return_value=stamp),
        ):
            results = DoctorCheck().check_deposited_guide()
        assert len(results) == 1
        assert results[0].passed is False
        assert results[0].symbol == "⚠"
        assert "unstamped" in results[0].message
        assert "vox enable" in results[0].message


# ---------------------------------------------------------------------------
# check_claude_desktop
# ---------------------------------------------------------------------------


class TestCheckClaudeDesktop:
    def test_config_not_found(self, tmp_path: Path) -> None:
        fake_path = tmp_path / "nonexistent" / "config.json"
        with patch(
            "punt_vox.desktop_install.DesktopInstaller.config_path",
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
            "punt_vox.desktop_install.DesktopInstaller.config_path",
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
            "punt_vox.desktop_install.DesktopInstaller.config_path",
            return_value=config,
        ):
            results = DoctorCheck().check_claude_desktop()
        assert len(results) == 2
        assert results[1].passed is False
        assert "not registered" in results[1].message


# ---------------------------------------------------------------------------
# CheckResults.format
# ---------------------------------------------------------------------------


class TestCheckResultsFormat:
    """CheckResults owns the render into (JSON payload, display text)."""

    def test_all_pass(self) -> None:
        results = CheckResults(
            [
                CheckResult.ok("a ok"),
                CheckResult.ok("b ok"),
            ]
        )
        payload, text = results.format()
        assert payload["passed"] == 2
        assert payload["failed"] == 0
        assert "2 passed, 0 failed" in text

    def test_one_fail(self) -> None:
        results = CheckResults([CheckResult.fail("bad")])
        payload, _text = results.format()
        assert payload["failed"] == 1

    def test_warnings_counted(self) -> None:
        results = CheckResults([CheckResult.warn("warning msg")])
        payload, text = results.format()
        assert payload["warned"] == 1
        assert "1 warning" in text

    def test_len_and_iter(self) -> None:
        entries = [CheckResult.ok("a"), CheckResult.fail("b")]
        results = CheckResults(entries)
        assert len(results) == 2
        assert list(results) == entries


class TestCheckResultConstructors:
    """The four alternate constructors set the wire fields consistently."""

    def test_ok(self) -> None:
        r = CheckResult.ok("all good")
        assert r.passed is True
        assert r.symbol == "✓"
        assert r.status_kind == "pass"
        assert r.required is True

    def test_fail(self) -> None:
        r = CheckResult.fail("nope")
        assert r.passed is False
        assert r.symbol == "✗"
        assert r.status_kind == "fail"
        assert r.required is True

    def test_warn(self) -> None:
        r = CheckResult.warn("careful")
        assert r.passed is False
        assert r.symbol == "⚠"
        assert r.status_kind == "warn"

    def test_of_optional_row(self) -> None:
        r = CheckResult.of("○", "uvx: not found", required=False)
        assert r.passed is False
        assert r.symbol == "○"
        assert r.status_kind == "skip"
        assert r.required is False

    def test_of_ok_symbol_marks_pass(self) -> None:
        r = CheckResult.of("✓", "uvx: present", required=False)
        assert r.passed is True
        assert r.status_kind == "pass"


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
                "punt_vox.desktop_install.DesktopInstaller.config_path",
                return_value=config_path,
            ),
        ):
            results = DoctorCheck(client=mock_client).run_all()
        for r in results:
            assert which_path not in r.message  # no binary install path
            assert str(config_path) not in r.message  # no absolute config path
