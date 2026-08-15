"""Diagnostic health checks for the vox system.

:class:`DoctorCheck` runs each sub-check and gathers a list of
:class:`CheckResult` values. The result type and the render-into-JSON
collection (:class:`~punt_vox.doctor_result.CheckResults`) live in
:mod:`punt_vox.doctor_result`; the mpv sub-check lives in
:mod:`punt_vox.doctor_mpv`.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from typing import Self

from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.client_sync import VoxClientSync
from punt_vox.desktop_install import DesktopInstaller
from punt_vox.dirs import default_output_dir, find_repo_root
from punt_vox.doctor_mpv import MpvCheck
from punt_vox.doctor_result import OK, OPTIONAL, CheckResult
from punt_vox.guide_stamp import GuideStamp, GuideStampVerdict
from punt_vox.paths import installed_version
from punt_vox.types_provider import ProviderStatusPayload

# ``MPV_MIN_VERSION`` is not imported here after the doctor_mpv split (the
# constant is used only by :class:`MpvCheck`); doctor.py delegates the mpv
# verdict, so the direct import from ``punt_vox.voxd.programs.mpv`` that
# vox-w3f8 PR 3 re-added does not belong on this side of the split.

__all__ = ["DoctorCheck"]


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
        results.extend(self.check_provider_readiness())
        results.extend(self.check_env_overrides())
        results.extend(self.check_music_dir())
        results.append(self.check_uvx())
        results.extend(self.check_claude_desktop())
        results.extend(self.check_output_dir())
        results.extend(self.check_deposited_guide())
        return results

    # -- individual checks -------------------------------------------------

    def check_python_version(self) -> CheckResult:
        """Check Python >= 3.13."""
        v = sys.version_info
        version_str = f"{v.major}.{v.minor}.{v.micro}"
        if v >= (3, 13):
            return CheckResult.ok(f"Python {version_str}")
        return CheckResult.fail(
            f"Python {version_str} (requires 3.13+)"
            " — install from https://www.python.org/downloads/"
        )

    def check_ffmpeg(self) -> CheckResult:
        """Check ffmpeg is installed -- present/absent verdict, no install path.

        ``ffmpeg`` decodes and transcodes audio; a missing binary is a hard
        error that fails ``vox doctor``.
        """
        if shutil.which("ffmpeg") is None:
            hints: dict[str, str] = {
                "Darwin": "brew install ffmpeg",
                "Windows": "winget install --id Gyan.FFmpeg",
                "default": "see https://ffmpeg.org/download.html",
            }
            hint = hints.get(platform.system(), hints["default"])
            return CheckResult.fail(f"ffmpeg: not found — {hint}")
        return CheckResult.ok("ffmpeg: present")

    def check_mpv(self) -> CheckResult:
        """Check mpv is installed AND at or above the pinned minimum version.

        The check lives in :class:`~punt_vox.doctor_mpv.MpvCheck`; ``doctor``
        holds the run_all schedule and delegates the mpv verdict there.
        """
        return MpvCheck().run()

    def check_espeak_fallback(self) -> list[CheckResult]:
        """Check espeak on Linux when no cloud API keys are set."""
        if platform.system() != "Linux":
            return []
        if any(os.environ.get(k) for k in ("ELEVENLABS_API_KEY", "OPENAI_API_KEY")):
            return []
        if shutil.which("espeak-ng") or shutil.which("espeak"):
            # Out of jail: report presence, never the binary's install path.
            return [CheckResult.ok("espeak: present (offline fallback)")]
        return [
            CheckResult.of(
                OPTIONAL,
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
                CheckResult.fail(
                    "Daemon: not running — start with 'vox daemon install'"
                )
            )
            return results
        except VoxdProtocolError as exc:
            results.append(CheckResult.fail(f"Daemon: reachable but unhealthy — {exc}"))
            return results

        port = health.port
        running_version = health.daemon_version
        wheel_version = installed_version()

        if running_version and running_version != wheel_version:
            results.append(
                CheckResult.warn(
                    f"Daemon: running on port {port} (version {running_version}"
                    f" — wheel has {wheel_version},"
                    f" run 'vox daemon restart' to refresh)"
                )
            )
        else:
            # Provider readiness lives on the ``provider_status`` op (design
            # §3.6, delivered by PR 3); the daemon has no provider of its
            # own, so the health line reports the version-and-port fact only.
            version_note = f", version {running_version}" if running_version else ""
            results.append(
                CheckResult.ok(f"Daemon: running on port {port}{version_note}")
            )
        return results

    def check_provider_readiness(self) -> list[CheckResult]:
        """Report the daemon's readiness verdict for every registered provider.

        Every F2 error message points here (``... run `vox doctor```),
        so this is where those pointers are answered.  The daemon owns
        the verdict -- ``ProviderCredentials.report_all`` walks the
        same requirement dispatch the resolution gate uses, so a
        provider that ``vox doctor`` says is ready is exactly a
        provider ``mic:unmute`` will not refuse for F2 reasons, and
        vice versa (§3.4).  A ready provider is a green pass; an
        unready one is a warning (optional) rather than a hard fail
        because a single-provider host is a normal configuration --
        only the state-declared provider being unavailable is a hard
        failure, and that surfaces the moment the caller tries to
        synthesize.

        Voxd unreachable is reported once at the section head and
        the per-provider walk is skipped, since the answer requires
        the daemon's environment.  The caller-side environment is
        deliberately NOT probed -- an earlier draft did, and it was
        the D1 wrong-process defect (§3.5).
        """
        client = self._client or VoxClientSync()
        try:
            payload = client.provider_status()
        except VoxdConnectionError:
            # Daemon down is already a fail from ``check_daemon_health``;
            # here it would be a duplicate hard-fail with the same cause.
            # Report it as informational so the readiness section
            # explains why it is empty rather than pretending nothing
            # was asked.
            return [
                CheckResult.of(
                    OPTIONAL,
                    "Provider readiness: skipped (voxd not running)",
                    required=False,
                )
            ]
        except VoxdProtocolError as exc:
            return [CheckResult.warn(f"Provider readiness: unavailable — {exc}")]
        return self._render_provider_readiness(payload)

    @staticmethod
    def _render_provider_readiness(
        payload: ProviderStatusPayload,
    ) -> list[CheckResult]:
        """Turn a ``provider_status`` payload into per-provider check lines.

        ``preferred`` leads the section so the user sees the daemon's
        proposed default before the walk of individual verdicts; a
        ``None`` ``preferred`` (no provider on this host is ready --
        no keys, no platform binary) is itself a hard fail because it
        is a genuinely unusable configuration for TTS.
        """
        results: list[CheckResult] = []
        if payload.preferred is None:
            results.append(
                CheckResult.fail(
                    "Provider readiness: no provider is usable on this daemon"
                    " — set at least one credential (e.g. ELEVENLABS_API_KEY,"
                    " OPENAI_API_KEY, or AWS credentials)"
                )
            )
        else:
            results.append(
                CheckResult.ok(f"Provider readiness: preferred → {payload.preferred}")
            )
        for row in payload.providers:
            if row.ready:
                results.append(CheckResult.ok(f"  {row.name}: ready"))
            else:
                # ``detail`` names the missing variables (F2) or is empty for
                # ``unknown_provider`` (F4); either way it is the same string
                # the wire error would carry, so ``doctor`` and ``mic:unmute``
                # cannot tell the user two different reasons.
                message = f"  {row.name}: not ready"
                if row.detail:
                    message = f"{message} — {row.detail}"
                results.append(CheckResult.of(OPTIONAL, message, required=False))
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
            return [CheckResult.ok(f"Remote config: {', '.join(overrides)}")]
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
            return [
                CheckResult.warn(
                    "output music dir: absent — created on first 'vox record'"
                )
            ]
        return []

    def check_uvx(self) -> CheckResult:
        """Check for uvx -- present/absent verdict, no host path (out of jail)."""
        if shutil.which("uvx"):
            return CheckResult.of(OK, "uvx: present", required=False)
        return CheckResult.of(
            OPTIONAL,
            "uvx: not found (needed for MCP server)",
            required=False,
        )

    def check_claude_desktop(self) -> list[CheckResult]:
        """Check Claude Desktop config and MCP registration."""
        results: list[CheckResult] = []
        config_path = DesktopInstaller.config_path()

        if not config_path.exists():
            results.append(
                CheckResult.of(
                    OPTIONAL,
                    "Claude Desktop config: not found",
                    required=False,
                )
            )
            results.append(
                CheckResult.of(
                    OPTIONAL,
                    "Claude Desktop MCP: not registered (run 'vox desktop install')",
                    required=False,
                )
            )
            return results

        # Out of jail (under ~/Library, neither data root): present/absent
        # verdict only -- the absolute config path never crosses to a client.
        results.append(
            CheckResult.of(OK, "Claude Desktop config: present", required=False)
        )

        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            servers = data.get("mcpServers", {})
            if "vox" in servers:
                results.append(
                    CheckResult.of(
                        OK,
                        "Claude Desktop MCP: registered",
                        required=False,
                    )
                )
            else:
                results.append(
                    CheckResult.of(
                        OPTIONAL,
                        "Claude Desktop MCP: not registered"
                        " (run 'vox desktop install')",
                        required=False,
                    )
                )
        except (json.JSONDecodeError, OSError):
            results.append(
                CheckResult.of(
                    OPTIONAL,
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
                return [CheckResult.ok("output: writable")]
            except OSError:
                return [CheckResult.fail("output: not writable — check permissions")]
        return [CheckResult.warn("output: absent — created on first 'vox record'")]

    def check_deposited_guide(self) -> list[CheckResult]:
        """Check the per-repo deposited guide against the packaged asset.

        The deposited guide (``.punt-labs/vox/CLAUDE.md``) is @-imported by the
        repo's own ``CLAUDE.md``, so it is what every agent working in the repo
        actually reads. Nothing else surfaces whether it has fallen behind the
        packaged source; a stale copy silently teaches agents tools that have
        been retired. This check reads the source-hash stamp
        (:class:`~punt_vox.guide_stamp.GuideStamp`) embedded on deposit and
        compares it to a fresh hash of the packaged asset.

        Four verdicts, distinct on purpose:

        * outside a repo, or in a repo with no deposited guide (vox not
          enabled): return an empty list -- not applicable, not a failure;
        * ``AGREE``: pass -- the deposited copy matches the packaged asset;
        * ``ABSENT_STAMP``: warn -- a copy from before this stamping existed
          (or one hand-edited beyond recognition). Unknown, not a false pass;
        * ``DIVERGE``: fail -- the deposit is provably behind the packaged
          source and re-``enable`` is needed.
        """
        root = find_repo_root()
        if root is None:
            return []
        deposited = root / ".punt-labs" / "vox" / "CLAUDE.md"
        # ``is_file`` swallows ENOENT / ENOTDIR / EBADF / ELOOP but propagates
        # OSError variants like PermissionError (e.g. an unreadable parent
        # directory) -- the same class of failure the deposit-read guard in
        # ``GuideStamp.read`` handles. A check whose job is to say the deposit
        # is in a bad state must not crash the whole ``vox doctor`` run when
        # it hits one; treat "cannot see the deposit" as not-applicable, the
        # same as "vox not enabled here".
        try:
            deposited_present = deposited.is_file()
        except OSError:
            return []
        if not deposited_present:
            return []
        verdict = GuideStamp.for_packaged_asset().verify(deposited)
        return [self._verdict_to_result(verdict)]

    @staticmethod
    def _verdict_to_result(verdict: GuideStampVerdict) -> CheckResult:
        """Turn a :class:`GuideStampVerdict` into the matching check line."""
        remediation = " — run 'vox enable' (or mic:enablement action=enable) to refresh"
        if verdict is GuideStampVerdict.AGREE:
            return CheckResult.ok("deposited guide: up to date")
        if verdict is GuideStampVerdict.DIVERGE:
            return CheckResult.fail(f"deposited guide: out of date{remediation}")
        return CheckResult.warn(
            f"deposited guide: unstamped, freshness unknown{remediation}"
        )
