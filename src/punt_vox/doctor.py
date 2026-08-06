"""Diagnostic health checks for the vox system."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.client_sync import VoxClientSync
from punt_vox.dirs import default_output_dir
from punt_vox.paths import installed_version
from punt_vox.voxd.programs.mpv import MPV_MIN_VERSION

__all__ = [
    "CheckResult",
    "DoctorCheck",
]

# ---------------------------------------------------------------------------
# Display constants
# ---------------------------------------------------------------------------

_OK = "✓"
_FAIL = "✗"
_OPTIONAL = "○"
_WARN = "⚠"

_STATUS_KIND: dict[str, str] = {
    _OK: "pass",
    _FAIL: "fail",
    _OPTIONAL: "skip",
    _WARN: "warn",
}

# ---------------------------------------------------------------------------
# Required host binaries
# ---------------------------------------------------------------------------

# The authoritative minimum mpv version lives with the mpv program player
# (``MPV_MIN_VERSION`` in ``punt_vox.voxd.programs.mpv``): the IPC command set,
# the ``end-file`` reason values, and the per-file ``pause`` load option hold
# only at or above it. ``doctor`` imports that one source of truth and derives
# the display string from it.
_MPV_MIN_STR: str = ".".join(str(part) for part in MPV_MIN_VERSION)

# Per-platform remediation hints. ``default`` covers any host not named.
_FFMPEG_HINTS: dict[str, str] = {
    "Darwin": "brew install ffmpeg",
    "Windows": "winget install --id Gyan.FFmpeg",
    "default": "see https://ffmpeg.org/download.html",
}
_MPV_HINTS: dict[str, str] = {
    "Darwin": "brew install mpv",
    "Linux": "sudo apt-get install mpv (or dnf/pacman)",
    "Windows": "see https://mpv.io/installation/",
    "default": "see https://mpv.io/installation/",
}
# Absence hints keyed by binary, so the absence verdict needs only the name.
_REQUIRED_HINTS: dict[str, dict[str, str]] = {
    "ffmpeg": _FFMPEG_HINTS,
    "mpv": _MPV_HINTS,
}


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Outcome of a single diagnostic check."""

    name: str
    passed: bool
    message: str
    detail: str = ""
    required: bool = True
    symbol: str = _OK
    status_kind: str = "pass"


# ---------------------------------------------------------------------------
# DoctorCheck
# ---------------------------------------------------------------------------


class DoctorCheck:
    """Run all diagnostic checks for the vox system."""

    __slots__ = ("_client",)
    _client: VoxClientSync | None

    def __new__(cls, client: VoxClientSync | None = None) -> Self:
        self = super().__new__(cls)
        self._client = client
        return self

    # -- public API --------------------------------------------------------

    def run_all(self) -> list[CheckResult]:
        """Execute every check and return results in order."""
        results: list[CheckResult] = []
        results.append(self.check_python_version())
        results.append(self.check_ffmpeg())
        results.append(self.check_mpv())
        results.extend(self.check_espeak_fallback())
        results.extend(self.check_daemon_health())
        results.extend(self.check_env_overrides())
        results.extend(self.check_music_dir())
        results.append(self.check_uvx())
        results.extend(self.check_claude_desktop())
        results.extend(self.check_output_dir())
        return results

    # -- individual checks -------------------------------------------------

    def check_python_version(self) -> CheckResult:
        """Check Python >= 3.13."""
        v = sys.version_info
        version_str = f"{v.major}.{v.minor}.{v.micro}"
        if v >= (3, 13):
            return _pass(f"Python {version_str}")
        return _fail(
            f"Python {version_str} (requires 3.13+)"
            " — install from https://www.python.org/downloads/"
        )

    def check_ffmpeg(self) -> CheckResult:
        """Check ffmpeg is installed -- present/absent verdict, no install path.

        ``ffmpeg`` decodes and transcodes audio; a missing binary is a hard
        error that fails ``vox doctor``.
        """
        if shutil.which("ffmpeg") is None:
            return self._missing_binary("ffmpeg")
        return _pass("ffmpeg: present")

    def check_mpv(self) -> CheckResult:
        """Check mpv is installed AND at or above the pinned minimum version.

        ``mpv`` plays the program audio tier (music, and later audiobooks and
        podcasts) over its JSON IPC socket. It is a hard dependency with no
        fallback -- notifications keep the built-in ``afplay``/``say``/
        ``espeak``, but program audio needs ``mpv``, and the IPC contract (the
        command set, the ``end-file`` reasons, the per-file ``pause`` option)
        holds only at or above ``MPV_MIN_VERSION`` (docs/mpv-program-player.md
        §1). A missing OR too-old binary is a hard error that fails
        ``vox doctor``.
        """
        if shutil.which("mpv") is None:
            return self._missing_binary("mpv")
        return self._check_mpv_version()

    def _missing_binary(self, name: str) -> CheckResult:
        """Verdict for an absent required host binary -- no host path leaks.

        ``ffmpeg`` and ``mpv`` are both out of jail (host binary locations), so
        the reply is a verdict, never the ``which`` path. Absence is a hard
        error (a red ``✗``): both are required with no fallback. The remediation
        hint is resolved from ``_REQUIRED_HINTS`` by name, so the verdict needs
        only the binary name.
        """
        hints = _REQUIRED_HINTS[name]
        hint = hints.get(platform.system(), hints["default"])
        return _fail(f"{name}: not found — {hint}")

    def _check_mpv_version(self) -> CheckResult:
        """Gate an installed mpv against ``MPV_MIN_VERSION``.

        A present mpv whose ``--version`` cannot be read, or that is older than
        the pinned minimum the program player's IPC contract needs, is a hard
        error carrying a per-platform upgrade hint -- the versioned form of the
        hard dependency (docs/mpv-program-player.md §1).
        """
        version = self._mpv_version()
        if version is None:
            return _fail(
                "mpv: present but version unreadable —"
                f" verify 'mpv --version' is >= {_MPV_MIN_STR}"
            )
        detected = ".".join(str(part) for part in version)
        if version < MPV_MIN_VERSION:
            hint = _MPV_HINTS.get(platform.system(), _MPV_HINTS["default"])
            return _fail(f"mpv {detected}: too old (needs >= {_MPV_MIN_STR}) — {hint}")
        return _pass(f"mpv: present ({detected})")

    def _mpv_version(self) -> tuple[int, int, int] | None:
        # ``None`` is the documented "cannot determine" outcome at this
        # subprocess boundary (mpv vanished from PATH mid-check, a broken
        # binary, a timeout, or unparseable output). The caller surfaces it as
        # a failing check, so this is absence-as-contract, not a value a caller
        # must defensively treat as success (PY-TS-14). The binary is resolved
        # to an absolute path first, mirroring the provider subprocess callers.
        mpv_path = shutil.which("mpv")
        if mpv_path is None:
            return None
        try:
            proc = subprocess.run(
                [mpv_path, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return self._parse_mpv_version(proc.stdout)

    @staticmethod
    def _parse_mpv_version(output: str) -> tuple[int, int, int] | None:
        # mpv prints ``mpv <major>.<minor>.<patch> Copyright ...`` on line one;
        # some builds prefix a ``v`` or append ``-git-<hash>``. ``None`` when no
        # version token is present (absence-as-contract, see ``_mpv_version``).
        match = re.search(r"\bmpv\s+v?(\d+)\.(\d+)(?:\.(\d+))?", output)
        if match is None:
            return None
        return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))

    def check_espeak_fallback(self) -> list[CheckResult]:
        """Check espeak on Linux when no cloud API keys are set."""
        if platform.system() != "Linux":
            return []
        if any(os.environ.get(k) for k in ("ELEVENLABS_API_KEY", "OPENAI_API_KEY")):
            return []
        if shutil.which("espeak-ng") or shutil.which("espeak"):
            # Out of jail: report presence, never the binary's install path.
            return [_pass("espeak: present (offline fallback)")]
        return [
            _result(
                _OPTIONAL,
                "espeak-ng/espeak: not found — install for offline TTS:"
                " sudo apt-get install espeak-ng",
                required=False,
            )
        ]

    def check_daemon_health(self) -> list[CheckResult]:
        """Check voxd daemon is running and version matches."""
        results: list[CheckResult] = []
        client = self._client or VoxClientSync()
        try:
            health = client.health()
        except VoxdConnectionError:
            results.append(
                _fail("Daemon: not running — start with 'vox daemon install'")
            )
            return results
        except VoxdProtocolError as exc:
            results.append(_fail(f"Daemon: reachable but unhealthy — {exc}"))
            return results

        provider_name = health.provider
        port = health.port
        running_version = health.daemon_version
        wheel_version = installed_version()

        if running_version and running_version != wheel_version:
            results.append(
                _warn(
                    f"Daemon: running on port {port} (version {running_version}"
                    f" — wheel has {wheel_version},"
                    f" run 'vox daemon restart' to refresh)"
                )
            )
        else:
            version_note = f", version {running_version}" if running_version else ""
            results.append(
                _pass(
                    f"Daemon: running on port {port}"
                    f" (provider: {provider_name}{version_note})"
                )
            )
        return results

    def check_env_overrides(self) -> list[CheckResult]:
        """Report active VOXD_* environment variable overrides."""
        overrides: list[str] = []
        for env_name in ("VOXD_HOST", "VOXD_PORT", "VOXD_TOKEN"):
            env_val = os.environ.get(env_name, "").strip()
            if env_val:
                display = "***" if env_name == "VOXD_TOKEN" else env_val
                overrides.append(f"{env_name}={display}")
        if overrides:
            return [_pass(f"Remote config: {', '.join(overrides)}")]
        return []

    def check_music_dir(self) -> list[CheckResult]:
        """Check the music dir -- present/absent verdict, no abs path.

        An in-jail path under the ``output`` root, reported as a relative verdict
        so its absolute prefix never crosses to a client.
        """
        from punt_vox.dirs import (
            _resolve_music_dir,  # pyright: ignore[reportPrivateUsage]
        )

        music_dir = _resolve_music_dir()  # pyright: ignore[reportPrivateUsage]
        if not music_dir.is_dir():
            return [_warn("output music dir: absent — created on first 'vox record'")]
        return []

    def check_uvx(self) -> CheckResult:
        """Check for uvx -- present/absent verdict, no host path (out of jail)."""
        if shutil.which("uvx"):
            return _result(_OK, "uvx: present", required=False)
        return _result(
            _OPTIONAL,
            "uvx: not found (needed for MCP server)",
            required=False,
        )

    def check_claude_desktop(self) -> list[CheckResult]:
        """Check Claude Desktop config and MCP registration."""
        results: list[CheckResult] = []
        config_path = claude_desktop_config_path()

        if not config_path.exists():
            results.append(
                _result(
                    _OPTIONAL,
                    "Claude Desktop config: not found",
                    required=False,
                )
            )
            results.append(
                _result(
                    _OPTIONAL,
                    "Claude Desktop MCP: not registered (run 'vox install-desktop')",
                    required=False,
                )
            )
            return results

        # Out of jail (under ~/Library, neither data root): present/absent
        # verdict only -- the absolute config path never crosses to a client.
        results.append(_result(_OK, "Claude Desktop config: present", required=False))

        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            servers = data.get("mcpServers", {})
            if "vox" in servers:
                results.append(
                    _result(
                        _OK,
                        "Claude Desktop MCP: registered",
                        required=False,
                    )
                )
            else:
                results.append(
                    _result(
                        _OPTIONAL,
                        "Claude Desktop MCP: not registered"
                        " (run 'vox install-desktop')",
                        required=False,
                    )
                )
        except (json.JSONDecodeError, OSError):
            results.append(
                _result(
                    _OPTIONAL,
                    "Claude Desktop MCP: could not read config",
                    required=False,
                )
            )
        return results

    def check_output_dir(self) -> list[CheckResult]:
        """Check the output dir -- writable verdict, no abs path or raw OSError.

        The dir *is* the ``output`` data root, so its own label is the name; the
        absolute path and the raw ``OSError`` stay out of the client-facing reply.
        """
        out_dir = default_output_dir()
        if out_dir.is_dir():
            try:
                test_file = out_dir / ".doctor_test"
                test_file.write_text("ok")
                test_file.unlink()
                return [_pass("output: writable")]
            except OSError:
                return [_fail("output: not writable — check permissions")]
        return [_warn("output: absent — created on first 'vox record'")]


# ---------------------------------------------------------------------------
# Helpers shared between DoctorCheck and __main__.py
# ---------------------------------------------------------------------------


def claude_desktop_config_path() -> Path:
    """Return the Claude Desktop config file path."""
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "Claude"
        / "claude_desktop_config.json"
    )


def format_results(results: list[CheckResult]) -> tuple[dict[str, object], str]:
    """Format check results into JSON payload and display text.

    Returns a (payload, text) tuple matching the existing ``doctor``
    command output format.
    """
    passed = 0
    failed = 0
    warned = 0
    lines: list[str] = []
    checks: list[dict[str, object]] = []

    for r in results:
        lines.append(f"{r.symbol} {r.message}")
        checks.append(
            {
                "status": r.symbol,
                "status_kind": r.status_kind,
                "message": r.message,
                "required": r.required,
                "passed": r.passed,
            }
        )
        if r.passed:
            passed += 1
        elif r.symbol == _FAIL and r.required:
            failed += 1
        elif r.symbol == _WARN:
            warned += 1

    summary = f"{passed} passed, {failed} failed"
    if warned > 0:
        summary += f", {warned} warning" + ("s" if warned > 1 else "")
    text_parts = ["=" * 40, *lines, "=" * 40, summary]

    payload: dict[str, object] = {
        "passed": passed,
        "failed": failed,
        "warned": warned,
        "checks": checks,
    }
    return payload, "\n".join(text_parts)


# ---------------------------------------------------------------------------
# Private result constructors
# ---------------------------------------------------------------------------


def _pass(message: str) -> CheckResult:
    """Create a passing check result."""
    return CheckResult(
        name=message,
        passed=True,
        message=message,
        symbol=_OK,
        status_kind="pass",
    )


def _fail(message: str) -> CheckResult:
    """Create a failing check result."""
    return CheckResult(
        name=message,
        passed=False,
        message=message,
        symbol=_FAIL,
        status_kind="fail",
    )


def _warn(message: str) -> CheckResult:
    """Create a warning check result."""
    return CheckResult(
        name=message,
        passed=False,
        message=message,
        symbol=_WARN,
        status_kind="warn",
    )


def _result(symbol: str, message: str, *, required: bool = True) -> CheckResult:
    """Create a check result with an explicit symbol."""
    return CheckResult(
        name=message,
        passed=symbol == _OK,
        message=message,
        symbol=symbol,
        status_kind=_STATUS_KIND.get(symbol, "fail"),
        required=required,
    )
