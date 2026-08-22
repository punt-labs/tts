"""End-to-end behavioural tests for ``install.sh`` skip resolution.

``install.sh`` cannot be unit-tested in-process, so these tests run the real
script under a sandboxed ``PATH`` of stub executables that log their argv. The
stubs make every external command (``uv``, ``claude``, ``git``, ``ssh``,
``vox``) succeed without touching the network or the real system, so the test
observes exactly which steps ran.

The invariant under test (install-cli-only.md): ``--no-plugin`` and
``VOX_NO_PLUGIN=1`` skip ONLY the marketplace-register + plugin-install steps
(the ``claude plugin ...`` calls); the binary install, daemon, and the
user-scope guide import (``vox register-guidance``) always run. An unknown flag
exits 2; the default (no flag) installs the plugin.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

_INSTALL_SH = Path(__file__).resolve().parent.parent / "install.sh"

# Every test spawns a real `sh install.sh` subprocess under a sandboxed PATH.
pytestmark = pytest.mark.subprocess

# Each stub logs its own name + argv to $VOX_TEST_LOG, then behaves just enough
# for install.sh to proceed. `claude plugin list` must echo the installed
# plugin id so the post-install verification grep passes; `ssh` must report
# "successfully authenticated" so the HTTPS-rewrite fallback (and any real
# `git config --global` write) never fires.
_STUBS: dict[str, str] = {
    "uv": '#!/bin/sh\nprintf "uv %s\\n" "$*" >> "$VOX_TEST_LOG"\nexit 0\n',
    "claude": (
        "#!/bin/sh\n"
        'printf "claude %s\\n" "$*" >> "$VOX_TEST_LOG"\n'
        'case "$*" in\n'
        '  "plugin list") echo "vox@punt-labs" ;;\n'
        "esac\n"
        "exit 0\n"
    ),
    "git": '#!/bin/sh\nprintf "git %s\\n" "$*" >> "$VOX_TEST_LOG"\nexit 0\n',
    # `mpv --version` must echo a parseable version line so install.sh's version
    # gate (mirroring doctor.py `_check_mpv_version`) can read it. The reported
    # version comes from MPV_FAKE_VERSION (set by the sandbox); an empty value
    # simulates a build whose `--version` carries no version token (unreadable).
    "mpv": (
        "#!/bin/sh\n"
        'printf "mpv %s\\n" "$*" >> "$VOX_TEST_LOG"\n'
        'case "$*" in\n'
        "  *--version*)\n"
        '    if [ -n "${MPV_FAKE_VERSION-}" ]; then\n'
        '      echo "mpv ${MPV_FAKE_VERSION} Copyright (c) test build"\n'
        "    else\n"
        '      echo "mpv (unknown build)"\n'
        "    fi\n"
        "    ;;\n"
        "esac\n"
        "exit 0\n"
    ),
    "ssh": (
        "#!/bin/sh\n"
        'printf "ssh %s\\n" "$*" >> "$VOX_TEST_LOG"\n'
        'echo "successfully authenticated"\n'
        "exit 0\n"
    ),
    "vox": '#!/bin/sh\nprintf "vox %s\\n" "$*" >> "$VOX_TEST_LOG"\nexit 0\n',
}


@dataclass(frozen=True)
class InstallRun:
    """The observable result of one sandboxed ``install.sh`` invocation."""

    returncode: int
    stdout: str
    stderr: str
    log: str

    def ran(self, fragment: str) -> bool:
        """Return whether any stub logged an argv line containing ``fragment``."""
        return fragment in self.log


class InstallSandbox:
    """A stubbed ``PATH`` and workdir for running ``install.sh`` harmlessly."""

    _bin: Path
    _work: Path
    _xdg: Path
    _log: Path

    def __new__(cls, root: Path) -> InstallSandbox:
        self = super().__new__(cls)
        self._bin = root / "bin"
        self._work = root / "work"
        self._xdg = root / "xdg"
        self._log = root / "invocations.log"
        for directory in (self._bin, self._work, self._xdg):
            directory.mkdir(parents=True, exist_ok=True)
        self._write_stubs()
        return self

    def _write_stubs(self) -> None:
        for name, body in _STUBS.items():
            path = self._bin / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)

    def run(
        self,
        *args: str,
        no_plugin_env: str | None = None,
        mpv_version: str = "0.38.0",
    ) -> InstallRun:
        """Run ``install.sh`` with ``args`` under the sandbox, return the result.

        ``no_plugin_env`` sets ``VOX_NO_PLUGIN`` to the given value; ``None``
        leaves it unset. ``mpv_version`` is the version the stub ``mpv``
        reports from ``--version`` (default a value well above the pinned
        minimum so the version gate passes); an empty string makes the stub
        emit a build line with no version token, exercising the unreadable
        path. Only the stub ``bin`` is on ``PATH`` besides the base system
        dirs, and ``XDG_DATA_HOME`` points at an empty temp tree so the
        root-owned-``__pycache__`` cleanup (which would ``sudo``) never fires.
        """
        if self._log.exists():
            self._log.unlink()
        env = {
            "PATH": f"{self._bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(self._work),
            "XDG_DATA_HOME": str(self._xdg),
            "VOX_TEST_LOG": str(self._log),
            "MPV_FAKE_VERSION": mpv_version,
            "TERM": "dumb",
        }
        if no_plugin_env is not None:
            env["VOX_NO_PLUGIN"] = no_plugin_env
        proc = subprocess.run(
            ["sh", str(_INSTALL_SH), *args],
            cwd=self._work,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        log = self._log.read_text(encoding="utf-8") if self._log.exists() else ""
        return InstallRun(proc.returncode, proc.stdout, proc.stderr, log)


@pytest.fixture
def sandbox(tmp_path: Path) -> InstallSandbox:
    return InstallSandbox(tmp_path)


class TestPluginInstallDefault:
    """With no flag and both capabilities present, the plugin is installed."""

    def test_default_installs_plugin(self, sandbox: InstallSandbox) -> None:
        result = sandbox.run()
        assert result.returncode == 0, result.stderr
        assert result.ran("claude plugin install vox@punt-labs")
        assert result.ran("claude plugin marketplace")

    def test_default_still_installs_cli_and_guide(
        self, sandbox: InstallSandbox
    ) -> None:
        result = sandbox.run()
        assert result.ran("uv tool install")
        assert result.ran("vox register-guidance")


class TestNoPluginFlag:
    """``--no-plugin`` skips only the marketplace + plugin-install steps."""

    def test_flag_skips_plugin_steps(self, sandbox: InstallSandbox) -> None:
        result = sandbox.run("--no-plugin")
        assert result.returncode == 0, result.stderr
        assert not result.ran("claude plugin install")
        assert not result.ran("claude plugin marketplace")

    def test_flag_still_installs_cli_daemon_and_guide(
        self, sandbox: InstallSandbox
    ) -> None:
        # The whole point: a --no-plugin box still gets a working CLI and its
        # ~/.punt-labs/vox/CLAUDE.md @-import (register-guidance).
        result = sandbox.run("--no-plugin")
        assert result.ran("uv tool install")
        assert result.ran("vox daemon install")
        assert result.ran("vox register-guidance")

    def test_flag_message_is_cli_only(self, sandbox: InstallSandbox) -> None:
        result = sandbox.run("--no-plugin")
        assert "vox CLI is ready" in result.stdout
        assert "Restart Claude Code" not in result.stdout


class TestNoPluginEnv:
    """``VOX_NO_PLUGIN`` honours the flag semantics, but only for exactly ``1``."""

    def test_env_one_skips_plugin(self, sandbox: InstallSandbox) -> None:
        result = sandbox.run(no_plugin_env="1")
        assert result.returncode == 0, result.stderr
        assert not result.ran("claude plugin install")
        assert result.ran("uv tool install")
        assert result.ran("vox register-guidance")

    def test_env_non_one_value_is_ignored(self, sandbox: InstallSandbox) -> None:
        # Any value other than exactly "1" installs the plugin (default path).
        result = sandbox.run(no_plugin_env="true")
        assert result.returncode == 0, result.stderr
        assert result.ran("claude plugin install vox@punt-labs")


class TestMpvVersionGate:
    """The mpv gate enforces MPV_MIN_VERSION, not mere presence.

    ``vox doctor`` (doctor.py ``_check_mpv_version``) fails an mpv older than
    the pinned ``MPV_MIN_VERSION`` (0.35.0). The installer must fail at the
    same tier so a box that passes install also passes doctor -- otherwise a
    distro shipping an older mpv installs "successfully" then cannot play
    program audio. The gate runs BEFORE the package install (Step 4), so a
    too-old or unreadable mpv stops the install before ``uv tool install``.
    """

    def test_new_enough_mpv_passes_and_installs(self, sandbox: InstallSandbox) -> None:
        result = sandbox.run(mpv_version="0.38.0")
        assert result.returncode == 0, result.stderr
        assert result.ran("uv tool install")

    def test_exact_minimum_mpv_passes(self, sandbox: InstallSandbox) -> None:
        # 0.35.0 is the pinned floor; equal must pass (>=, not >).
        result = sandbox.run(mpv_version="0.35.0")
        assert result.returncode == 0, result.stderr
        assert result.ran("uv tool install")

    def test_too_old_mpv_fails_loudly_before_install(
        self, sandbox: InstallSandbox
    ) -> None:
        result = sandbox.run(mpv_version="0.34.0")
        assert result.returncode == 1
        assert "too old" in result.stdout
        assert "0.35.0" in result.stdout
        # Hard-tier failure: it stops before the package install (Step 4).
        assert not result.ran("uv tool install")

    def test_unreadable_mpv_version_fails_before_install(
        self, sandbox: InstallSandbox
    ) -> None:
        # An mpv whose --version carries no version token is a hard failure,
        # not a silent pass.
        result = sandbox.run(mpv_version="")
        assert result.returncode == 1
        assert "unreadable" in result.stdout
        assert not result.ran("uv tool install")


class TestArgumentErrors:
    """Unknown flags are a usage error; --help is informational."""

    def test_unknown_flag_exits_2(self, sandbox: InstallSandbox) -> None:
        result = sandbox.run("--no-plguin")
        assert result.returncode == 2
        assert "unknown option: --no-plguin" in result.stderr
        assert "Usage:" in result.stderr
        # It must exit before touching the system — nothing installed.
        assert not result.ran("uv tool install")

    def test_help_exits_zero(self, sandbox: InstallSandbox) -> None:
        result = sandbox.run("--help")
        assert result.returncode == 0
        assert "Usage:" in result.stdout
