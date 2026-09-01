"""Tests for punt_vox.service.process — process management."""
# pyright: reportUnknownLambdaType=false

from __future__ import annotations

import logging
import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from punt_vox.service import DEFAULT_PORT
from punt_vox.service.installer import ServiceInstaller
from punt_vox.service.process import ProcessManager

# ---------------------------------------------------------------------------
# Exec args
# ---------------------------------------------------------------------------


def test_voxd_exec_args() -> None:
    args = ServiceInstaller._voxd_exec_args()
    assert args[0].endswith("voxd")
    assert "--port" in args
    assert str(DEFAULT_PORT) in args


def test_voxd_exec_args_resolves_relative_to_sys_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_voxd_exec_args must use sys.executable/../voxd, not $PATH."""
    bin_dir = tmp_path / "fake-dist" / "bin"
    bin_dir.mkdir(parents=True)
    fake_voxd = bin_dir / "voxd"
    fake_voxd.write_text("#!/bin/sh\necho fake voxd")
    fake_voxd.chmod(0o755)
    fake_python = bin_dir / "python"
    fake_python.write_text('#!/bin/sh\nexec /usr/bin/python3 "$@"')
    fake_python.chmod(0o755)

    monkeypatch.setattr("punt_vox.service.installer.sys.executable", str(fake_python))
    args = ServiceInstaller._voxd_exec_args()
    assert args[0] == str(fake_voxd)
    assert "--port" in args


def test_voxd_exec_args_ignores_stale_voxd_on_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale voxd earlier on $PATH must not be baked into ExecStart."""
    current = tmp_path / "current" / "bin"
    current.mkdir(parents=True)
    (current / "voxd").write_text("current")
    (current / "voxd").chmod(0o755)
    (current / "python").write_text("#!/bin/sh\n")
    (current / "python").chmod(0o755)

    stale_dir = tmp_path / "stale" / "bin"
    stale_dir.mkdir(parents=True)
    (stale_dir / "voxd").write_text("stale")
    (stale_dir / "voxd").chmod(0o755)

    monkeypatch.setenv("PATH", f"{stale_dir}:{current}:/usr/bin:/bin")
    monkeypatch.setattr(
        "punt_vox.service.installer.sys.executable",
        str(current / "python"),
    )

    args = ServiceInstaller._voxd_exec_args()
    assert args[0] == str(current / "voxd"), (
        "Expected sys.executable-relative voxd, "
        f"got {args[0]} — stale binary from PATH leaked in."
    )


def test_voxd_exec_args_missing_binary_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing voxd next to sys.executable must raise SystemExit."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python"
    fake_python.write_text("#!/bin/sh\n")
    fake_python.chmod(0o755)

    monkeypatch.setattr("punt_vox.service.installer.sys.executable", str(fake_python))
    with pytest.raises(SystemExit, match="voxd binary not found"):
        ServiceInstaller._voxd_exec_args()


def test_voxd_exec_args_rejects_non_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A voxd file without the executable bit must raise SystemExit."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python"
    fake_python.write_text("#!/bin/sh\n")
    fake_python.chmod(0o755)
    voxd = bin_dir / "voxd"
    voxd.write_text("#!/bin/sh\n")
    voxd.chmod(0o644)

    monkeypatch.setattr("punt_vox.service.installer.sys.executable", str(fake_python))
    with pytest.raises(SystemExit, match="not executable"):
        ServiceInstaller._voxd_exec_args()


def test_voxd_exec_args_rejects_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directory at the voxd path must raise SystemExit, not succeed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python"
    fake_python.write_text("#!/bin/sh\n")
    fake_python.chmod(0o755)
    (bin_dir / "voxd").mkdir()

    monkeypatch.setattr("punt_vox.service.installer.sys.executable", str(fake_python))
    with pytest.raises(SystemExit, match="voxd binary not found"):
        ServiceInstaller._voxd_exec_args()


# ---------------------------------------------------------------------------
# _find_pid_on_port
# ---------------------------------------------------------------------------


@patch("punt_vox.service.process.platform.system", return_value="Darwin")
@patch("punt_vox.service.process.subprocess.run")
def test_find_pid_on_port_macos(mock_run: MagicMock, _mock_sys: MagicMock) -> None:
    mgr = ProcessManager()
    mock_run.return_value = MagicMock(returncode=0, stdout="12345\n")
    assert mgr.find_pid_on_port(8421) == [12345]
    mock_run.assert_called_once_with(
        ["lsof", "-ti", ":8421"], capture_output=True, text=True, timeout=5
    )


@patch("punt_vox.service.process.platform.system", return_value="Darwin")
@patch("punt_vox.service.process.subprocess.run")
def test_find_pid_on_port_macos_multiple(
    mock_run: MagicMock, _mock_sys: MagicMock
) -> None:
    mgr = ProcessManager()
    mock_run.return_value = MagicMock(returncode=0, stdout="12345\n67890\n")
    assert mgr.find_pid_on_port(8421) == [12345, 67890]


@patch("punt_vox.service.process.platform.system", return_value="Darwin")
@patch("punt_vox.service.process.subprocess.run")
def test_find_pid_on_port_ignores_non_decimal_digits(
    mock_run: MagicMock, _mock_sys: MagicMock
) -> None:
    """Superscript digits pass str.isdigit but break int(); isdecimal drops them."""
    # "²" (superscript two): isdigit() is True, isdecimal() is False.
    mock_run.return_value = MagicMock(returncode=0, stdout="123\n²\n456\n")
    assert ProcessManager().find_pid_on_port(8421) == [123, 456]


@patch("punt_vox.service.process.platform.system", return_value="Linux")
@patch("punt_vox.service.process.subprocess.run")
def test_find_pid_on_port_linux(mock_run: MagicMock, _mock_sys: MagicMock) -> None:
    mgr = ProcessManager()
    mock_run.return_value = MagicMock(returncode=0, stdout="8421/tcp:  6789\n")
    assert mgr.find_pid_on_port(8421) == [6789]
    mock_run.assert_called_once_with(
        ["fuser", "8421/tcp"], capture_output=True, text=True, timeout=5
    )


@patch("punt_vox.service.process.platform.system", return_value="Darwin")
@patch("punt_vox.service.process.subprocess.run")
def test_find_pid_on_port_empty_when_not_bound(
    mock_run: MagicMock, _mock_sys: MagicMock
) -> None:
    mgr = ProcessManager()
    mock_run.return_value = MagicMock(returncode=1, stdout="")
    assert mgr.find_pid_on_port(8421) == []


@patch("punt_vox.service.process.platform.system", return_value="Darwin")
@patch(
    "punt_vox.service.process.subprocess.run",
    side_effect=subprocess.TimeoutExpired(cmd="lsof", timeout=5),
)
def test_find_pid_on_port_timeout(_mock_run: MagicMock, _mock_sys: MagicMock) -> None:
    mgr = ProcessManager()
    assert mgr.find_pid_on_port(8421) == []


def test_find_pid_on_port_timeout_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A probe timeout must warn -- a stalled tool can't read as an empty port."""
    with (
        patch("punt_vox.service.process.platform.system", return_value="Darwin"),
        patch(
            "punt_vox.service.process.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="lsof", timeout=5),
        ),
        caplog.at_level(logging.WARNING, logger="punt_vox.service.process"),
    ):
        assert ProcessManager().find_pid_on_port(8421) == []

    messages = [rec.getMessage() for rec in caplog.records]
    assert any("lsof" in m for m in messages)


def test_find_pid_on_port_tool_failure_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A missing probe binary logs a WARNING naming the tool and the error."""
    boom = FileNotFoundError(2, "No such file or directory")
    with (
        patch("punt_vox.service.process.platform.system", return_value="Darwin"),
        patch("punt_vox.service.process.subprocess.run", side_effect=boom),
        caplog.at_level(logging.WARNING, logger="punt_vox.service.process"),
    ):
        assert ProcessManager().find_pid_on_port(8421) == []

    messages = [rec.getMessage() for rec in caplog.records]
    assert any("lsof" in m for m in messages)
    assert any("No such file or directory" in m for m in messages)


def test_find_pid_on_port_empty_does_not_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A genuinely empty port (tool ran, exit 1) must stay quiet -- no warning."""
    with (
        patch("punt_vox.service.process.platform.system", return_value="Darwin"),
        patch(
            "punt_vox.service.process.subprocess.run",
            return_value=MagicMock(returncode=1, stdout=""),
        ),
        caplog.at_level(logging.WARNING, logger="punt_vox.service.process"),
    ):
        assert ProcessManager().find_pid_on_port(8421) == []

    assert not [rec for rec in caplog.records if rec.levelno >= logging.WARNING]


# ---------------------------------------------------------------------------
# _kill_pid
# ---------------------------------------------------------------------------


@patch("punt_vox.service.process.os.kill")
def test_kill_pid_exits_after_sigterm(mock_kill: MagicMock) -> None:
    mgr = ProcessManager()
    mock_kill.side_effect = [None, ProcessLookupError]
    assert mgr.kill_pid(100) is True
    assert mock_kill.call_args_list[0] == call(100, signal.SIGTERM)
    assert mock_kill.call_args_list[1] == call(100, 0)


@patch("punt_vox.service.process.os.kill")
def test_kill_pid_already_gone(mock_kill: MagicMock) -> None:
    mgr = ProcessManager()
    mock_kill.side_effect = ProcessLookupError
    assert mgr.kill_pid(100) is True
    mock_kill.assert_called_once_with(100, signal.SIGTERM)


@patch("punt_vox.service.process.os.kill", side_effect=PermissionError)
def test_kill_pid_permission_error(mock_kill: MagicMock) -> None:
    mgr = ProcessManager()
    assert mgr.kill_pid(100) is False
    mock_kill.assert_called_once_with(100, signal.SIGTERM)


@patch("punt_vox.service.process.time.sleep")
@patch("punt_vox.service.process.time.monotonic")
@patch("punt_vox.service.process.os.kill")
def test_kill_pid_sigkill_after_timeout(
    mock_kill: MagicMock, mock_monotonic: MagicMock, _mock_sleep: MagicMock
) -> None:
    mgr = ProcessManager()
    mock_kill.side_effect = [None, None, None, ProcessLookupError]
    mock_monotonic.side_effect = [0.0, 0.0, 6.0, 6.0, 6.0]
    assert mgr.kill_pid(100) is True
    assert mock_kill.call_args_list == [
        call(100, signal.SIGTERM),
        call(100, 0),
        call(100, signal.SIGKILL),
        call(100, 0),
    ]


# ---------------------------------------------------------------------------
# kill_stale_daemon
# ---------------------------------------------------------------------------


@patch.object(ProcessManager, "remove_port_file")
@patch.object(ProcessManager, "kill_pid", return_value=True)
@patch.object(ProcessManager, "is_vox_daemon_process", return_value=True)
@patch.object(ProcessManager, "find_pid_on_port", return_value=[999])
@patch.object(ProcessManager, "read_port_file", return_value=8421)
def test_kill_stale_daemon_kills_process(
    _mock_port: MagicMock,
    mock_find: MagicMock,
    _mock_is_vox: MagicMock,
    mock_kill: MagicMock,
    mock_remove: MagicMock,
) -> None:
    mgr = ProcessManager()
    assert mgr.kill_stale_daemon() is True
    mock_find.assert_called_once_with(8421)
    mock_kill.assert_called_once_with(999)
    mock_remove.assert_called_once()


@patch.object(ProcessManager, "find_pid_on_port", return_value=[])
@patch.object(ProcessManager, "read_port_file", return_value=None)
def test_kill_stale_daemon_no_process(
    _mock_port: MagicMock, _mock_find: MagicMock
) -> None:
    mgr = ProcessManager()
    assert mgr.kill_stale_daemon() is False


@patch.object(ProcessManager, "remove_port_file")
@patch.object(ProcessManager, "kill_pid", return_value=True)
@patch.object(ProcessManager, "is_vox_daemon_process", return_value=True)
@patch.object(ProcessManager, "find_pid_on_port", return_value=[555])
@patch.object(ProcessManager, "read_port_file", return_value=None)
def test_kill_stale_daemon_uses_default_port(
    _mock_port: MagicMock,
    mock_find: MagicMock,
    _mock_is_vox: MagicMock,
    mock_kill: MagicMock,
    _mock_remove: MagicMock,
) -> None:
    mgr = ProcessManager()
    assert mgr.kill_stale_daemon() is True
    mock_find.assert_called_once_with(DEFAULT_PORT)


@patch.object(ProcessManager, "kill_pid")
@patch.object(ProcessManager, "is_vox_daemon_process", return_value=False)
@patch.object(ProcessManager, "find_pid_on_port", return_value=[999])
@patch.object(ProcessManager, "read_port_file", return_value=8421)
def test_kill_stale_daemon_skips_non_vox_process(
    _mock_port: MagicMock,
    _mock_find: MagicMock,
    _mock_is_vox: MagicMock,
    mock_kill: MagicMock,
) -> None:
    mgr = ProcessManager()
    assert mgr.kill_stale_daemon() is False
    mock_kill.assert_not_called()


@patch.object(ProcessManager, "remove_port_file")
@patch.object(ProcessManager, "kill_pid", return_value=True)
@patch.object(ProcessManager, "is_vox_daemon_process", side_effect=[False, True])
@patch.object(ProcessManager, "find_pid_on_port", return_value=[100, 200])
@patch.object(ProcessManager, "read_port_file", return_value=8421)
def test_kill_stale_daemon_iterates_pids(
    _mock_port: MagicMock,
    _mock_find: MagicMock,
    mock_is_vox: MagicMock,
    mock_kill: MagicMock,
    mock_remove: MagicMock,
) -> None:
    """First PID is a client (not vox), second is the daemon -- kills second."""
    mgr = ProcessManager()
    assert mgr.kill_stale_daemon() is True
    assert mock_is_vox.call_count == 2
    mock_kill.assert_called_once_with(200)
    mock_remove.assert_called_once()


@patch.object(ProcessManager, "remove_port_file")
@patch.object(ProcessManager, "kill_pid", return_value=False)
@patch.object(ProcessManager, "is_vox_daemon_process", return_value=True)
@patch.object(ProcessManager, "find_pid_on_port", return_value=[999])
@patch.object(ProcessManager, "read_port_file", return_value=8421)
def test_kill_stale_daemon_no_cleanup_on_kill_failure(
    _mock_port: MagicMock,
    _mock_find: MagicMock,
    _mock_is_vox: MagicMock,
    mock_kill: MagicMock,
    mock_remove: MagicMock,
) -> None:
    """When kill_pid fails, state files are NOT removed."""
    mgr = ProcessManager()
    assert mgr.kill_stale_daemon() is False
    mock_kill.assert_called_once_with(999)
    mock_remove.assert_not_called()


# ---------------------------------------------------------------------------
# _is_vox_daemon_process
# ---------------------------------------------------------------------------


@patch("punt_vox.service.process.subprocess.run")
def test_is_vox_daemon_process_true(mock_run: MagicMock) -> None:
    mgr = ProcessManager()
    mock_run.return_value = MagicMock(
        stdout="/usr/bin/python3 -m punt_vox serve --port 8421"
    )
    assert mgr.is_vox_daemon_process(123) is True


@patch("punt_vox.service.process.subprocess.run")
def test_is_vox_daemon_process_hyphen_path(mock_run: MagicMock) -> None:
    """Matches when cmd contains punt-vox (hyphen) but not punt_vox."""
    mgr = ProcessManager()
    cmd = "/home/<user>/.local/share/uv/tools/punt-vox/bin/vox serve --port 8421"
    mock_run.return_value = MagicMock(stdout=cmd)
    assert mgr.is_vox_daemon_process(123) is True


@patch("punt_vox.service.process.subprocess.run")
def test_is_vox_daemon_process_bare_vox_binary(mock_run: MagicMock) -> None:
    """Matches when started as bare ``vox serve`` without punt_vox in path."""
    mgr = ProcessManager()
    mock_run.return_value = MagicMock(
        stdout="/Users/someone/.local/bin/vox serve --port 8421"
    )
    assert mgr.is_vox_daemon_process(123) is True


@patch("punt_vox.service.process.subprocess.run")
def test_is_vox_daemon_process_false(mock_run: MagicMock) -> None:
    mgr = ProcessManager()
    mock_run.return_value = MagicMock(stdout="nginx: master process")
    assert mgr.is_vox_daemon_process(123) is False


@patch(
    "punt_vox.service.process.subprocess.run",
    side_effect=subprocess.TimeoutExpired(cmd="ps", timeout=5),
)
def test_is_vox_daemon_process_timeout(_mock_run: MagicMock) -> None:
    mgr = ProcessManager()
    assert mgr.is_vox_daemon_process(123) is False


# ---------------------------------------------------------------------------
# ensure_port_free
# ---------------------------------------------------------------------------


@patch.object(ProcessManager, "find_pid_on_port", return_value=[1234])
@patch.object(ProcessManager, "kill_stale_daemon", return_value=False)
def test_ensure_port_free_raises_when_occupied(
    _mock_kill: MagicMock,
    _mock_find: MagicMock,
) -> None:
    """ensure_port_free raises SystemExit when port is still occupied after kill."""
    with pytest.raises(SystemExit, match="still in use"):
        ProcessManager().ensure_port_free()


@patch.object(ProcessManager, "find_pid_on_port", return_value=[])
@patch.object(ProcessManager, "kill_stale_daemon", return_value=False)
def test_ensure_port_free_succeeds_when_clear(
    _mock_kill: MagicMock,
    _mock_find: MagicMock,
) -> None:
    """ensure_port_free succeeds when no PIDs on port after kill."""
    ProcessManager().ensure_port_free()  # Should not raise


def test_ensure_port_free_uses_port_file_not_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-kill port re-check targets the port file, not ``DEFAULT_PORT``."""
    captured: list[int] = []

    def fake_find(self: ProcessManager, port: int) -> list[int]:
        captured.append(port)
        return []

    monkeypatch.setattr(ProcessManager, "read_port_file", lambda self: 9999)
    monkeypatch.setattr(ProcessManager, "kill_stale_daemon", lambda self: True)
    monkeypatch.setattr(ProcessManager, "find_pid_on_port", fake_find)

    ProcessManager().ensure_port_free()

    assert captured == [9999], (
        f"ensure_port_free checked the wrong port: expected [9999], got {captured}"
    )


def test_ensure_port_free_error_message_reports_actual_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SystemExit message must name the ACTUAL port, not DEFAULT_PORT."""
    monkeypatch.setattr(ProcessManager, "read_port_file", lambda self: 9999)
    monkeypatch.setattr(ProcessManager, "kill_stale_daemon", lambda self: False)
    monkeypatch.setattr(ProcessManager, "find_pid_on_port", lambda self, port: [5555])

    with pytest.raises(SystemExit, match="Port 9999 is still in use"):
        ProcessManager().ensure_port_free()


def test_ensure_port_free_falls_back_to_default_when_port_file_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No port file on disk: fall back to ``DEFAULT_PORT`` for the re-check."""
    captured: list[int] = []

    def fake_find(self: ProcessManager, port: int) -> list[int]:
        captured.append(port)
        return []

    monkeypatch.setattr(ProcessManager, "read_port_file", lambda self: None)
    monkeypatch.setattr(ProcessManager, "kill_stale_daemon", lambda self: False)
    monkeypatch.setattr(ProcessManager, "find_pid_on_port", fake_find)

    ProcessManager().ensure_port_free()

    assert captured == [DEFAULT_PORT]


def test_ensure_port_free_reads_port_file_before_kill_stale_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Port file read MUST happen BEFORE ``kill_stale_daemon`` runs.

    Regression guard for Cursor Bugbot on PR #175.
    """
    monkeypatch.setattr(ProcessManager, "_run_dir", staticmethod(lambda: tmp_path))
    port_file = tmp_path / "serve.port"
    port_file.write_text("9999\n", encoding="utf-8")

    kill_called: list[bool] = []

    def spy_kill(self: ProcessManager) -> bool:
        kill_called.append(True)
        # Simulate the real kill_stale_daemon happy-path: port file removed.
        self.remove_port_file()
        return True

    captured_port: list[int] = []

    def spy_find(self: ProcessManager, port: int) -> list[int]:
        captured_port.append(port)
        return []

    monkeypatch.setattr(ProcessManager, "kill_stale_daemon", spy_kill)
    monkeypatch.setattr(ProcessManager, "find_pid_on_port", spy_find)

    ProcessManager().ensure_port_free()

    assert kill_called == [True]
    assert captured_port == [9999], (
        f"find_pid_on_port was called with {captured_port}, not [9999]."
    )
    assert DEFAULT_PORT not in captured_port
