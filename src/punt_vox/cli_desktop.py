"""The ``vox desktop`` CLI verbs -- install and uninstall the MCP registration.

Nested subcommand group matching ``vox daemon`` (single-verb subcommand names,
cli.md §Subcommand naming; ``install-desktop`` was retired for this form).
:class:`DesktopCli` is a humble object: each verb parses ``--json`` /
``--verbose`` / ``--quiet``, resolves the Claude Desktop config path via
:meth:`DesktopInstaller.config_path`, drives :class:`DesktopInstaller` for the
non-secret ``env`` map, and formats via the shared :class:`OutputFormatter`.
``install`` and ``uninstall`` are symmetric so nothing the CLI can register
requires manual editing of ``claude_desktop_config.json`` to undo.

Which platforms can be registered is not a question this module answers. It
asks :meth:`DesktopInstaller.config_path` for a path and reports the refusal
it gets back; the installer owns the per-platform location, so the CLI's
verdict and doctor's read-back cannot disagree about where the file lives.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self, cast, final

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

    # What the Claude Desktop app itself writes, and what a file holding other
    # MCP servers' credentials should be: owner-only, on the file and on the
    # directory vox creates when the app has not run yet.
    _CONFIG_MODE: ClassVar[int] = 0o600
    _CONFIG_DIR_MODE: ClassVar[int] = 0o700

    def __new__(cls, formatter: OutputFormatter, flags: OutputFlags) -> Self:
        self = super().__new__(cls)
        self._formatter = formatter
        self._flags = flags
        return self

    def _load_config(self, config_path: Path) -> dict[str, Any]:
        """Read a Claude Desktop config JSON object, or ``{}`` if absent.

        A non-object top level (list/string) would crash deep inside the
        caller's ``setdefault`` merge; rejected here with a clean Typer error.
        Bytes that are not UTF-8 fail in ``read_text`` rather than in
        ``json.loads``, so ``UnicodeDecodeError`` is caught alongside the
        parse and I/O failures -- it is the same "unreadable config" verdict.
        """
        if not config_path.exists():
            return {}
        try:
            parsed = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            detail = f"Could not read {config_path}: {exc}"
            self._formatter.error(detail, f"Error: {detail}")
            raise typer.Exit(code=1) from exc
        if not isinstance(parsed, dict):
            detail = f"{config_path} must be a JSON object."
            self._formatter.error(detail, f"Error: {detail}")
            raise typer.Exit(code=1)
        return cast("dict[str, Any]", parsed)

    def _server_map(self, config_path: Path, data: dict[str, Any]) -> dict[str, Any]:
        """Return the config's ``mcpServers`` object, creating it when absent.

        A non-object value under that key is the case that bites silently:
        ``"vox" in servers`` against a string is a *substring* test, so a
        hand-edited ``"mcpServers": "vox"`` reads as an existing registration
        and the assignment right after it raises. Rejected here with the same
        clean Typer error a non-object top level gets.
        """
        servers = data.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            detail = f'{config_path} must have a JSON object under "mcpServers".'
            self._formatter.error(detail, f"Error: {detail}")
            raise typer.Exit(code=1)
        return cast("dict[str, Any]", servers)

    def _write_config(self, config_path: Path, data: dict[str, Any]) -> None:
        """Replace the config, or refuse with a clean error.

        Symmetric with :meth:`_load_config`: a filesystem failure at this
        boundary becomes one Typer error line, not a traceback. Text that
        cannot be encoded to UTF-8 fails in the write rather than in
        ``json.dumps``, so ``UnicodeError`` is caught alongside the I/O
        failures -- it is the same "unwritable config" verdict.
        """
        try:
            self._replace_atomically(config_path, json.dumps(data, indent=2) + "\n")
        except (OSError, UnicodeError) as exc:
            detail = f"Could not write {config_path}: {exc}"
            self._formatter.error(detail, f"Error: {detail}")
            raise typer.Exit(code=1) from exc

    @classmethod
    def _replace_atomically(cls, config_path: Path, text: str) -> None:
        """Swap *text* in as the whole of *config_path* by rename.

        ``claude_desktop_config.json`` is not vox's file: every other MCP
        server keeps its own ``env`` block there, secrets included, and vox
        rewrites the whole document to change one key of it. A
        truncate-then-write that dies mid-stream therefore destroys *their*
        entries, not just vox's. The temp file is a sibling so the rename
        stays on one filesystem and stays atomic; it inherits ``mkstemp``'s
        0600 and carries it through, leaving the config owner-only the way the
        Claude Desktop app writes it. A failed write takes its temp file with
        it rather than leaving a stray dotfile behind.

        A symlinked config (chezmoi, stow) is written *through*: renaming onto
        the link replaces it with a regular file and strands the real target on
        the old registration, which the owning tool restores on its next apply.
        """
        target = config_path.resolve() if config_path.is_symlink() else config_path
        fd, tmp_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            tmp_path.chmod(cls._CONFIG_MODE)
            tmp_path.replace(target)
        except (OSError, UnicodeError):
            tmp_path.unlink(missing_ok=True)
            raise

    def _resolve_config_path(self) -> Path:
        """Return the Claude Desktop config path for this host, or refuse.

        The platform question is asked once, of :class:`DesktopInstaller`,
        which owns the per-platform location. A second ``platform.system()``
        probe here would be a rival verdict that drifts the moment Claude
        Desktop reaches another platform.
        """
        try:
            return DesktopInstaller.config_path()
        except ValueError as exc:
            self._formatter.error(str(exc), f"Error: {exc}")
            raise typer.Exit(code=1) from exc

    def _resolve_uvx(self, uvx_path: str | None) -> str:
        """Return the ``uvx`` binary Claude Desktop will launch, or refuse."""
        uvx = uvx_path or shutil.which("uvx")
        if not uvx:
            detail = "uvx not found. Install uv (https://docs.astral.sh/uv/) first."
            self._formatter.error(detail, f"Error: {detail}")
            raise typer.Exit(code=1)
        return uvx

    def _resolve_installer(
        self, provider: str | None, audio_dir: Path
    ) -> DesktopInstaller:
        """Build the installer for *provider*, or refuse when none is ready.

        ``detect(None, ...)`` raises when no provider credentials are in view
        of the installer: a fresh install with nothing to route to has no
        sensible default to write.
        """
        try:
            return DesktopInstaller.detect(provider, audio_dir)
        except ValueError as exc:
            self._formatter.error(str(exc), f"Error: {exc}")
            raise typer.Exit(code=1) from exc

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

        Every prerequisite is resolved before anything is created on disk, so
        a host vox cannot register with leaves no output directory behind.
        """
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)

        # Every refusal comes first: each _resolve_* raises Exit(1), so the
        # mkdirs below run only once nothing can decline. _resolve_installer
        # merely records audio_dir; it does not need the directory to exist.
        uvx = self._resolve_uvx(uvx_path)
        config_path = self._resolve_config_path()
        audio_dir = output_dir or default_output_dir()
        installer = self._resolve_installer(install_provider, audio_dir)

        audio_dir.mkdir(parents=True, exist_ok=True)
        config_path.parent.mkdir(
            parents=True, exist_ok=True, mode=self._CONFIG_DIR_MODE
        )
        data = self._load_config(config_path)
        servers = self._server_map(config_path, data)
        overwriting = "vox" in servers
        servers["vox"] = {
            "command": uvx,
            "args": ["--from", "punt-vox", "vox", "mcp"],
            "env": installer.server_env(),
        }
        self._write_config(config_path, data)

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

        config_path = self._resolve_config_path()
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
        self._write_config(config_path, data)
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
