"""Structural introspection of the vox Click command tree for CLI surface tests.

Rich soft-wraps the option-name column in rendered ``--help`` at the test
runner's 80-col width, so scraping a flag name out of ``result.output`` splits
it across lines -- the substring match then fails on Linux CI while passing on
macOS. The registered Click parameters are the platform-independent fact these
helpers expose, so surface tests assert against them rather than help text.
"""

from __future__ import annotations

import typer
import typer.main
from click import Group
from typer.core import TyperArgument, TyperGroup, TyperOption

from punt_vox.__main__ import app

# Typer 0.27 stopped inheriting TyperGroup from click.Group; group semantics
# now live on TyperGroup itself with its own `.commands` mapping. Accept either
# shape so introspection works across the older-Group and newer-TyperGroup
# layouts.
_GroupType = Group | TyperGroup


def app_help_texts(built: typer.Typer) -> list[str]:
    """Every help string a Typer app renders: the group help, each verb's help,
    and each verb's option/argument help.

    Rich wraps these when it paints ``--help``, so a substring assertion on the
    rendered table is width-fragile. The Click objects carry the unwrapped
    source strings, which is what a help-cleanliness test should assert against.
    """
    group = typer.main.get_command(built)
    texts = [group.help or ""]
    if isinstance(group, _GroupType):
        for command in group.commands.values():
            texts.append(command.help or "")
            texts.extend(
                param.help or ""
                for param in command.params
                if isinstance(param, TyperOption | TyperArgument)
            )
    return texts


def command_opts(*path: str) -> set[str]:
    """Every option string (long and short) on the app command at ``path``.

    ``command_opts("install-desktop")`` returns the top-level command's flags;
    ``command_opts("rec", "get")`` walks the ``rec`` group to its ``get`` verb.
    Raises ``LookupError`` if a name on the path is not a group.
    """
    command = typer.main.get_command(app)
    for name in path:
        if not isinstance(command, _GroupType):
            raise LookupError(f"{name!r}: parent is not a command group")
        command = command.commands[name]
    return {
        opt for param in command.params for opt in (*param.opts, *param.secondary_opts)
    }
