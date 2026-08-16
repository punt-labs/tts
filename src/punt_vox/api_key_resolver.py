"""Four-source mutual-exclusion API key resolution for the CLI."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Self

import typer

__all__ = ["ApiKeyResolver"]

_API_KEY_ARGV_WARNING = (
    "warning: --api-key on the command line is visible via 'ps' and "
    "shell history. Prefer VOX_API_KEY env var, --api-key-file <path>, "
    "or --api-key-stdin for real credentials."
)


class ApiKeyResolver:
    """Resolve a per-call API key from exactly one of four sources.

    Sources (mutually exclusive):
    1. ``--api-key-file <path>``
    2. ``--api-key-stdin``
    3. ``VOX_API_KEY`` env var
    4. ``--api-key <value>`` on argv
    """

    __slots__ = ("_api_key", "_api_key_file", "_api_key_stdin", "_ctx")

    _ctx: typer.Context
    _api_key: str | None
    _api_key_file: Path | None
    _api_key_stdin: bool

    def __new__(
        cls,
        ctx: typer.Context,
        api_key: str | None,
        api_key_file: Path | None,
        *,
        api_key_stdin: bool,
    ) -> Self:
        self = super().__new__(cls)
        self._ctx = ctx
        self._api_key = api_key
        self._api_key_file = api_key_file
        self._api_key_stdin = api_key_stdin
        return self

    def resolve(self) -> str | None:
        """Return the resolved API key, or None for anonymous calls."""
        self._check_mutual_exclusion()

        if self._api_key_file is not None:
            return self._read_file(self._api_key_file)
        if self._api_key_stdin:
            return self._read_stdin()
        if self._api_key is not None:
            source = self._ctx.get_parameter_source("api_key")
            # Typer 0.27 vendors its own click; compare enum by ``.name``.
            if getattr(source, "name", None) == "COMMANDLINE":
                typer.echo(_API_KEY_ARGV_WARNING, err=True)
            return self._api_key
        return None

    def _check_mutual_exclusion(self) -> None:
        """Raise if more than one source is set."""
        file_set = self._api_key_file is not None
        stdin_set = self._api_key_stdin
        argv_or_env_set = self._api_key is not None
        sources_set = int(file_set) + int(stdin_set) + int(argv_or_env_set)
        if sources_set <= 1:
            return

        named: list[str] = []
        if file_set:
            named.append("--api-key-file")
        if stdin_set:
            named.append("--api-key-stdin")
        if argv_or_env_set:
            source = self._ctx.get_parameter_source("api_key")
            src_name = getattr(source, "name", None)
            named.append("VOX_API_KEY" if src_name == "ENVIRONMENT" else "--api-key")
        conflict = ", ".join(named)
        msg = (
            f"Specify at most one API key source; got {conflict}. "
            "These are mutually exclusive."
        )
        raise typer.BadParameter(msg)

    @staticmethod
    def _read_file(path: Path) -> str:
        """Read a per-call API key from a file.

        Rejects missing paths, non-files, and empty files. Warns when
        file permissions are too broad (anything beyond 0600).
        """
        if not path.is_file():
            msg = f"--api-key-file: {path} is not a file"
            raise typer.BadParameter(msg)
        mode = path.stat().st_mode
        if mode & 0o077:
            typer.echo(
                f"warning: --api-key-file: {path} is accessible to group "
                f"or other users (mode {oct(mode & 0o777)}). Run "
                f"'chmod 600 {path}' to tighten permissions.",
                err=True,
            )
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            msg = f"--api-key-file: {path} is empty"
            raise typer.BadParameter(msg)
        return value

    @staticmethod
    def _read_stdin() -> str:
        """Read a per-call API key from stdin (one line)."""
        if sys.stdin.isatty():
            msg = "--api-key-stdin requires piped input (stdin is a tty)"
            raise typer.BadParameter(msg)
        line = sys.stdin.readline().strip()
        if not line:
            msg = "--api-key-stdin: received empty input"
            raise typer.BadParameter(msg)
        return line
