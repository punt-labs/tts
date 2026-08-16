"""The ``vox daemon`` CLI verbs -- install, uninstall, restart, status.

The daemon subcommand group manages the ``voxd`` system service on the local
host (macOS LaunchAgent / Linux systemd). :class:`DaemonCli` is a humble
object: each verb parses ``--json`` / ``--verbose`` / ``--quiet`` through the
shared :class:`OutputFlags`, dispatches to :mod:`punt_vox.service` or the
:class:`DaemonRestarter`, and formats via the shared :class:`OutputFormatter`.
Every subcommand answers ``--json`` (parity with ``vox status``, ``vox voices``,
``vox music``, ``vox rec``); ``vox daemon status --json`` emits the fields the
daemon reports over ``health()`` so a caller reads the same shape whether it
asks the CLI or drives the daemon directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Self, final

import typer

from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.client_sync import VoxClientSync
from punt_vox.daemon_restarter import DaemonRestarter
from punt_vox.service import install as svc_install, uninstall as svc_uninstall

if TYPE_CHECKING:
    from punt_vox.cli_io import OutputFlags
    from punt_vox.output_formatter import OutputFormatter

__all__ = ["DaemonCli", "build_daemon_app"]

# Redeclared per-verb so ``--json`` parses after the subcommand as well as
# before it (matching the music/rec/enablement groups).
_JsonOutput = Annotated[bool, typer.Option("--json", help="Output JSON.")]
_Verbose = Annotated[
    bool, typer.Option("--verbose", "-v", help="Enable debug logging.")
]
_Quiet = Annotated[
    bool, typer.Option("--quiet", "-q", help="Suppress non-JSON output.")
]


@final
class DaemonCli:
    """The ``vox daemon`` verbs bound onto a nested typer group.

    The output flags are shared with the top-level app (one accumulating
    :class:`OutputFlags`, one :class:`OutputFormatter`), so a ``--json`` passed
    at either position selects the same mode.
    """

    __slots__ = ("_flags", "_formatter")

    _formatter: OutputFormatter
    _flags: OutputFlags

    def __new__(cls, formatter: OutputFormatter, flags: OutputFlags) -> Self:
        self = super().__new__(cls)
        self._formatter = formatter
        self._flags = flags
        return self

    def install(
        self,
        *,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """Register vox as a system service (launchd/systemd).

        Run as your normal user, NOT under ``sudo``. On macOS no sudo is
        needed -- the LaunchAgent installs to ``~/Library/LaunchAgents/``.
        On Linux, vox will prompt once for your sudo password to place
        the systemd unit. Running under sudo yourself would cause per-user
        state to land under ``/root/.punt-labs/vox/`` -- wrong on both
        platforms.
        """
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        try:
            result = svc_install()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            self._formatter.emit(
                {"installed": False, "reason": str(exc)},
                f"Install failed: {exc}",
            )
            raise typer.Exit(code=code) from exc
        self._formatter.emit({"installed": True, "message": result}, result)

    def uninstall(
        self,
        *,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """Remove the vox system service.

        Symmetric with ``vox daemon install``. Stops the running voxd,
        removes the launchd plist (macOS) or systemd unit (Linux), and
        reports what was removed. Runs as your normal user; on Linux vox
        prompts once for your sudo password to touch the systemd unit.
        """
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        try:
            result = svc_uninstall()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            self._formatter.emit(
                {"uninstalled": False, "reason": str(exc)},
                f"Uninstall failed: {exc}",
            )
            raise typer.Exit(code=code) from exc
        self._formatter.emit({"uninstalled": True, "message": result}, result)

    def restart(
        self,
        *,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """Restart the voxd system service and verify it is back up.

        Use this after ``uv tool upgrade punt-vox`` so the running daemon
        picks up the new wheel. A plain ``uv tool upgrade`` replaces the
        on-disk binary but does not cycle the long-running voxd process --
        changes to the WebSocket protocol or playback behavior do not
        take effect until the service is restarted.

        Runs as your normal user, NOT under ``sudo``. On macOS, no sudo
        is needed (LaunchAgent). On Linux, vox will prompt once for your
        sudo password when it drives ``systemctl``.
        """
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        DaemonRestarter(self._formatter).run()

    def status(
        self,
        *,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """Show the voxd daemon's health -- reachable, port, uptime, version.

        Routes through the client so it queries the configured daemon --
        honoring ``VOXD_HOST`` / ``VOXD_PORT`` / ``VOXD_TOKEN`` -- rather than
        a hardcoded ``127.0.0.1``, matching ``vox status`` and ``vox doctor``.
        ``--json`` returns the daemon health snapshot (``status``, ``port``,
        ``pid``, ``provider``, ``daemon_version``, ``uptime_seconds``,
        ``queued``, ``active_sessions``) -- the same shape ``mic:status``
        reports for the daemon block.
        """
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        try:
            health = VoxClientSync().health()
        except (VoxdConnectionError, VoxdProtocolError) as exc:
            not_running: dict[str, object] = {
                "status": "not_running",
                "reason": str(exc),
            }
            self._formatter.emit(not_running, f"Daemon: not running ({exc})")
            raise typer.Exit(code=1) from exc

        payload: dict[str, object] = {
            "status": health.status,
            "port": health.port,
            "pid": health.pid,
            # ``provider`` is not on the health payload -- the daemon has
            # no provider of its own; per-provider readiness moves to the
            # ``provider_status`` op (design §3.6, delivered by PR 3).
            "daemon_version": health.daemon_version,
            "uptime_seconds": health.uptime_seconds,
            "queued": health.queued,
            "active_sessions": health.active_sessions,
        }
        text = (
            f"Daemon: {health.status} on port {health.port}\n"
            f"  Uptime:   {health.uptime_seconds}s\n"
            f"  Sessions: {health.active_sessions}"
        )
        self._formatter.emit(payload, text)


def build_daemon_app(formatter: OutputFormatter, flags: OutputFlags) -> typer.Typer:
    """Return the ``vox daemon`` Typer group with bound verbs (no wrappers)."""
    cli = DaemonCli(formatter, flags)
    app = typer.Typer(
        help="Manage the vox daemon service.",
        no_args_is_help=True,
    )
    app.command("install")(cli.install)
    app.command("uninstall")(cli.uninstall)
    app.command("restart")(cli.restart)
    app.command("status")(cli.status)
    return app
