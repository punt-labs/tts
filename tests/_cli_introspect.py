"""Structural introspection of the vox Click command tree for CLI surface tests.

Rich soft-wraps the option-name column in rendered ``--help`` at the test
runner's 80-col width, so scraping a flag name out of ``result.output`` splits
it across lines -- the substring match then fails on Linux CI while passing on
macOS. The registered Click parameters are the platform-independent fact these
helpers expose, so surface tests assert against them rather than help text.
"""

from __future__ import annotations

import typer.main
from click import Group

from punt_vox.__main__ import app


def command_opts(*path: str) -> set[str]:
    """Every option string (long and short) on the app command at ``path``.

    ``command_opts("install-desktop")`` returns the top-level command's flags;
    ``command_opts("rec", "get")`` walks the ``rec`` group to its ``get`` verb.
    Raises ``LookupError`` if a name on the path is not a group.
    """
    command = typer.main.get_command(app)
    for name in path:
        if not isinstance(command, Group):
            raise LookupError(f"{name!r}: parent is not a command group")
        command = command.commands[name]
    return {
        opt for param in command.params for opt in (*param.opts, *param.secondary_opts)
    }
