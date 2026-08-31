"""Tests for punt_vox.service.launchd -- macOS launchd backend."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from punt_vox.service import DEFAULT_PORT
from punt_vox.service.launchd import LaunchdBackend
from punt_vox.service.process import ProcessManager


def _backend_with_mock_agent(
    monkeypatch: pytest.MonkeyPatch,
    plist: Path,
) -> tuple[LaunchdBackend, MagicMock]:
    """Build a LaunchdBackend whose composed LaunchctlAgent is a MagicMock.

    launchd.py's contract after the LaunchctlAgent extraction is "author the
    plist, then delegate every launchctl call to the agent" -- including the
    lifecycle verbs (bootout/bootstrap/kickstart) and the status probe. These
    tests verify that delegation; the agent's own subprocess behavior
    (bootout/bootstrap race handling, the list probe) is covered in
    test_service_launchctl.py.
    """
    agent = MagicMock()

    def _make_agent(label: str, plist_path: str) -> MagicMock:
        return agent

    monkeypatch.setattr("punt_vox.service.launchd.LaunchctlAgent", _make_agent)
    monkeypatch.setattr("punt_vox.service.launchd._LAUNCHD_PLIST", plist)
    be = LaunchdBackend(
        ProcessManager(),
        lambda: ["/usr/local/bin/voxd", "--port", "8421"],
    )
    return be, agent


@pytest.fixture()
def backend() -> LaunchdBackend:
    """Create a LaunchdBackend with a stub exec-args function."""
    return LaunchdBackend(
        ProcessManager(),
        lambda: ["/usr/local/bin/voxd", "--port", "8421"],
    )


# ---------------------------------------------------------------------------
# launchd plist content
# ---------------------------------------------------------------------------


def test_launchd_plist_contains_label(backend: LaunchdBackend) -> None:
    content = backend.plist_content()
    assert "com.punt-labs.voxd" in content


def test_launchd_plist_contains_args(backend: LaunchdBackend) -> None:
    content = backend.plist_content()
    assert "voxd" in content
    assert str(DEFAULT_PORT) in content


def test_launchd_plist_has_no_file_log_redirect(backend: LaunchdBackend) -> None:
    """The plist must not tee daemon output to a second, unprotected log file.

    voxd logs once to the 0600 ``vox.log`` via its private file handler.
    ``StandardErrorPath`` would have launchd capture a duplicate, world-readable
    copy of the same records, defeating that file's private permissions.
    """
    content = backend.plist_content()
    assert "StandardOutPath" not in content
    assert "StandardErrorPath" not in content
    assert "voxd-stdout.log" not in content
    assert "voxd-stderr.log" not in content


def test_launchd_plist_keepalive(backend: LaunchdBackend) -> None:
    content = backend.plist_content()
    assert "<key>KeepAlive</key>" in content
    assert "<true/>" in content


def test_launchd_plist_has_process_type_interactive(backend: LaunchdBackend) -> None:
    """ProcessType=Interactive prevents App Nap throttling."""
    content = backend.plist_content()
    assert "<key>ProcessType</key>" in content
    assert "<string>Interactive</string>" in content


def test_launchd_plist_no_username_key(backend: LaunchdBackend) -> None:
    """LaunchAgents must not contain UserName -- invalid for agents."""
    content = backend.plist_content()
    assert "<key>UserName</key>" not in content


def test_launchd_plist_pins_to_aqua_session(backend: LaunchdBackend) -> None:
    """LimitLoadToSessionType=Aqua pins voxd to the graphical login session.

    Without the pin, launchd may bootstrap the agent into a Background
    context that lacks CoreAudio session membership -- producing the
    intermittent ``AudioQueueStart failed (-66681)`` after a ~15s block
    that made afplay silently fail dozens of times per session.
    """
    content = backend.plist_content()
    assert "<key>LimitLoadToSessionType</key>" in content
    # Value must appear on the line following the key (adjacent in the
    # emitted plist), not merely somewhere in the document.
    lines = content.splitlines()
    key_index = next(
        i for i, line in enumerate(lines) if "<key>LimitLoadToSessionType</key>" in line
    )
    assert "<string>Aqua</string>" in lines[key_index + 1]


@patch.dict("os.environ", {"PATH": "/opt/homebrew/bin:/usr/bin:/bin"})
def test_launchd_plist_contains_path_from_env() -> None:
    be = LaunchdBackend(
        ProcessManager(),
        lambda: ["/opt/homebrew/bin/voxd", "--port", "8421"],
    )
    content = be.plist_content()
    assert "<key>EnvironmentVariables</key>" in content
    assert "<key>PATH</key>" in content
    assert "/opt/homebrew/bin:/usr/bin:/bin" in content


@patch.dict(
    "os.environ",
    {
        "SSL_CERT_FILE": "/etc/ssl/certs/proxy-ca.pem",
        "REQUESTS_CA_BUNDLE": "/etc/ssl/certs/proxy-ca.pem",
    },
    clear=True,
)
def test_launchd_plist_bakes_the_ca_bundle_vars() -> None:
    """The CA vars reach the plist through the backend a user installs with.

    ``VoxdPlist`` is unit-tested directly; this pins the wiring, so a backend
    that stopped consulting the captured environment would fail here even
    while the plist class still passed its own tests.
    """
    be = LaunchdBackend(
        ProcessManager(),
        lambda: ["/usr/local/bin/voxd", "--port", "8421"],
    )
    content = be.plist_content()
    assert "<key>SSL_CERT_FILE</key>" in content
    assert "<key>REQUESTS_CA_BUNDLE</key>" in content
    assert "/etc/ssl/certs/proxy-ca.pem" in content


@patch.dict("os.environ", {}, clear=True)
def test_launchd_plist_omits_the_ca_bundle_vars_when_unset() -> None:
    be = LaunchdBackend(
        ProcessManager(),
        lambda: ["/usr/local/bin/voxd", "--port", "8421"],
    )
    content = be.plist_content()
    assert "<key>SSL_CERT_FILE</key>" not in content
    assert "<key>REQUESTS_CA_BUNDLE</key>" not in content


# ---------------------------------------------------------------------------
# stop -- delegates to the composed LaunchctlAgent
# ---------------------------------------------------------------------------


def test_launchd_stop_noop_when_plist_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh install (no prior plist): stop skips the bootout delegation."""
    plist = tmp_path / "com.punt-labs.voxd.plist"  # never created
    be, agent = _backend_with_mock_agent(monkeypatch, plist)
    be.stop()
    agent.bootout.assert_not_called()


def test_launchd_stop_delegates_bootout_when_plist_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing plist: stop delegates to the agent's race-free bootout."""
    plist = tmp_path / "com.punt-labs.voxd.plist"
    plist.write_text("<plist/>")
    be, agent = _backend_with_mock_agent(monkeypatch, plist)
    be.stop()
    agent.bootout.assert_called_once_with()


# ---------------------------------------------------------------------------
# install (fresh, no migration)
# ---------------------------------------------------------------------------


def test_launchd_install_writes_plist_and_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """install authors the plist, then delegates bring-up to agent.start()."""
    agents_dir = tmp_path / "Library" / "LaunchAgents"
    plist = agents_dir / "com.punt-labs.voxd.plist"
    monkeypatch.setattr("punt_vox.service.launchd._LAUNCHD_DIR", agents_dir)
    be, agent = _backend_with_mock_agent(monkeypatch, plist)

    be.install()

    assert plist.exists()
    agent.start.assert_called_once_with()


def test_launchd_reinstall_over_stale_agent_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second install over a live agent boots it out, then re-starts it."""
    agents_dir = tmp_path / "Library" / "LaunchAgents"
    plist = agents_dir / "com.punt-labs.voxd.plist"
    monkeypatch.setattr("punt_vox.service.launchd._LAUNCHD_DIR", agents_dir)
    be, agent = _backend_with_mock_agent(monkeypatch, plist)

    # First install writes the plist and starts.
    be.install()
    assert plist.exists()

    # Second install: a live agent exists, so stop() must bootout first.
    be.stop()
    be.install()

    assert agent.start.call_count == 2
    assert agent.bootout.call_count == 1
    # The plist survives the idempotent reinstall.
    assert plist.exists()


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


def test_launchd_uninstall_boots_out_and_removes_plist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """uninstall delegates bootout to the agent and unlinks the plist."""
    agents_dir = tmp_path / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True)
    plist = agents_dir / "com.punt-labs.voxd.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr("punt_vox.service.launchd._LAUNCHD_DIR", agents_dir)
    be, agent = _backend_with_mock_agent(monkeypatch, plist)

    with patch.object(
        ProcessManager, "kill_stale_daemon", return_value=True
    ) as mock_kill:
        result = be.uninstall()

    assert not plist.exists()
    agent.bootout.assert_called_once_with()
    mock_kill.assert_called_once()
    # uninstall must not discard the kill result — a live daemon that was
    # killed reports True.
    assert result is True


def test_launchd_uninstall_noop_when_plist_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """uninstall skips bootout when no plist exists but still kills stale daemon."""
    plist = tmp_path / "com.punt-labs.voxd.plist"  # never created
    be, agent = _backend_with_mock_agent(monkeypatch, plist)

    with patch.object(ProcessManager, "kill_stale_daemon") as mock_kill:
        be.uninstall()

    agent.bootout.assert_not_called()
    mock_kill.assert_called_once()


def test_launchd_uninstall_propagates_failed_kill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """uninstall returns False when the stale-daemon kill fails (survivor)."""
    agents_dir = tmp_path / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True)
    plist = agents_dir / "com.punt-labs.voxd.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr("punt_vox.service.launchd._LAUNCHD_DIR", agents_dir)
    be, _agent = _backend_with_mock_agent(monkeypatch, plist)

    with patch.object(ProcessManager, "kill_stale_daemon", return_value=False):
        result = be.uninstall()

    # A failed kill must not be swallowed — the caller re-scans on False.
    assert result is False
    # The plist is removed regardless of the kill outcome.
    assert not plist.exists()


# ---------------------------------------------------------------------------
# status -- delegates the launchctl list probe to the composed agent
# ---------------------------------------------------------------------------


def test_launchd_status_true_when_agent_reports_loaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """status returns True by delegating to the agent's list probe."""
    plist = tmp_path / "com.punt-labs.voxd.plist"
    be, agent = _backend_with_mock_agent(monkeypatch, plist)
    agent.is_loaded.return_value = True

    assert be.status() is True
    agent.is_loaded.assert_called_once_with()


def test_launchd_status_false_when_agent_reports_unloaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """status returns False when the agent reports the job is not loaded."""
    plist = tmp_path / "com.punt-labs.voxd.plist"
    be, agent = _backend_with_mock_agent(monkeypatch, plist)
    agent.is_loaded.return_value = False

    assert be.status() is False
    agent.is_loaded.assert_called_once_with()
