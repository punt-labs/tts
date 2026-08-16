"""The ``vox desktop`` CLI verbs -- install and uninstall the MCP registration.

Nested subcommand group matching ``vox daemon`` (single-verb subcommand names,
cli.md §Subcommand naming; ``install-desktop`` was retired for this form).
:class:`DesktopCli` is a humble object: each verb parses ``--json`` /
``--verbose`` / ``--quiet``, resolves the Claude Desktop config path via
:meth:`DesktopInstaller.config_path`, drives :class:`DesktopInstaller` for the
non-secret ``env`` map, and formats via the shared :class:`OutputFormatter`.
``install`` and ``uninstall`` are symmetric so nothing the CLI can register
requires manual editing of ``claude_desktop_config.json`` to undo.
"""

from __future__ import annotations

import json
import platform
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Self, cast, final

import typer

from punt_vox.desktop_install import DesktopInstaller
from punt_vox.dirs import default_output_dir

if TYPE_CHECKING:
    from punt_vox.cli_io import OutputFlags
    from punt_vox.output_formatter import OutputFormatter

__all__ = ["DesktopCli", "build_desktop_app"]

_JsonOutput = Annotated[bool, typer.Option("--json", help="Output JSON.")]
_Verbose = Annotated[
    bool, typer.Option("--verbose", "-v", help="Enable debug logging.")
]
_Quiet = Annotated[
    bool, typer.Option("--quiet", "-q", help="Suppress non-JSON output.")
]
_OutputDirOpt = Annotated[
    Path | None,
    typer.Option("--output-dir", "-d", help="Output directory. Default: ~/Music/vox."),
]
_UvxPathOpt = Annotated[
    str | None,
    typer.Option("--uvx-path", help="Path to uvx binary. Default: auto-detect."),
]
_ProviderOpt = Annotated[
    str | None,
    typer.Option("--provider", help="TTS provider. Default: auto-detect."),
]


@final
class DesktopCli:
    """The ``vox desktop`` verbs bound onto a nested typer group."""

    __slots__ = ("_flags", "_formatter")

    _formatter: OutputFormatter
    _flags: OutputFlags

    def __new__(cls, formatter: OutputFormatter, flags: OutputFlags) -> Self:
        self = super().__new__(cls)
        self._formatter = formatter
        self._flags = flags
        return self

    def _load_config(self, config_path: Path) -> dict[str, Any]:
        """Read a Claude Desktop config JSON object, or ``{}`` if absent.

        A non-object top level (list/string) would crash deep inside the
        caller's ``setdefault`` merge; rejected here with a clean Typer error.
        Errors route through :class:`OutputFormatter` so ``--json`` callers get
        an ``{"error": ...}`` envelope rather than plain-text stderr.
        """
        if not config_path.exists():
            return {}
        try:
            parsed = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            self._formatter.error(
                f"Could not read {config_path}: {exc}",
                f"Error: Could not read {config_path}: {exc}",
            )
            raise typer.Exit(code=1) from exc
        if not isinstance(parsed, dict):
            self._formatter.error(
                f"{config_path} must be a JSON object.",
                f"Error: {config_path} must be a JSON object.",
            )
            raise typer.Exit(code=1)
        return cast("dict[str, Any]", parsed)

    def install(
        self,
        output_dir: _OutputDirOpt = None,
        uvx_path: _UvxPathOpt = None,
        install_provider: _ProviderOpt = None,
        *,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """Register the MCP server with Claude Desktop.

        Writes routing config only -- provider name and output directory --
        to ``claude_desktop_config.json``. The provider API key never
        appears there; the daemon reads its key from ``keys.env`` at
        startup (PL-PP-4).
        """
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        if platform.system() != "Darwin":
            typer.echo(
                "Warning: Claude Desktop config path is only known for macOS. "
                "You may need to configure manually on this platform.",
                err=True,
            )

        uvx = uvx_path or shutil.which("uvx")
        if not uvx:
            typer.echo(
                "Error: uvx not found. Install uv (https://docs.astral.sh/uv/) first.",
                err=True,
            )
            raise typer.Exit(code=1)

        audio_dir = output_dir or default_output_dir()
        audio_dir.mkdir(parents=True, exist_ok=True)

        # ``detect(None, ...)`` raises ``ValueError`` when no provider
        # credentials are in view of the installer -- catch it and route
        # through the same ``typer.echo(err) + typer.Exit(1)`` convention
        # the rest of :class:`DesktopCli` uses for user-facing failures,
        # rather than letting a bare traceback reach the terminal.
        try:
            installer = DesktopInstaller.detect(install_provider, audio_dir)
        except ValueError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        config_path = DesktopInstaller.config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)

        data = self._load_config(config_path)
        servers = data.setdefault("mcpServers", {})
        overwriting = "vox" in servers
        servers["vox"] = {
            "command": uvx,
            "args": ["--from", "punt-vox", "vox", "mcp"],
            "env": installer.server_env(),
        }
        config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        payload: dict[str, object] = {
            "registered": True,
            "overwritten": overwriting,
            "provider": installer.provider,
            "config": str(config_path),
            "output": str(audio_dir),
        }
        lines = [
            "Updated existing vox entry."
            if overwriting
            else "Registered vox MCP server.",
            f"Provider: {installer.provider}",
            f"Config: {config_path}",
            f"Output: {audio_dir}",
            "Restart Claude Desktop to activate.",
        ]
        self._formatter.emit(payload, "\n".join(lines))
        if not installer.daemon_can_authenticate():
            typer.echo(installer.credential_guidance(), err=True)

    def uninstall(
        self,
        *,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """Remove the vox MCP server entry from the Claude Desktop config.

        Symmetric with ``vox desktop install``. The daemon and its
        ``keys.env`` are separate concerns owned by ``vox daemon
        uninstall``; this touches only ``claude_desktop_config.json``.
        Idempotent: absent config or absent ``vox`` entry both report
        "not registered" and exit 0.
        """
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)

        config_path = DesktopInstaller.config_path()
        if not config_path.exists():
            self._formatter.emit(
                {"unregistered": False, "reason": "config_absent"},
                f"Claude Desktop config not found at {config_path}; nothing to do.",
            )
            return

        data = self._load_config(config_path)
        servers = data.get("mcpServers")
        if not isinstance(servers, dict) or "vox" not in servers:
            self._formatter.emit(
                {"unregistered": False, "reason": "vox_absent"},
                "vox MCP server not registered; nothing to do.",
            )
            return

        del servers["vox"]
        config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        self._formatter.emit(
            {"unregistered": True, "config": str(config_path)},
            f"Removed vox MCP server from {config_path}.\n"
            "Restart Claude Desktop to apply.",
        )


def build_desktop_app(formatter: OutputFormatter, flags: OutputFlags) -> typer.Typer:
    """Return the ``vox desktop`` Typer group with bound verbs (no wrappers)."""
    cli = DesktopCli(formatter, flags)
    app = typer.Typer(
        help="Manage the Claude Desktop MCP registration.",
        no_args_is_help=True,
    )
    app.command("install")(cli.install)
    app.command("uninstall")(cli.uninstall)
    return app
