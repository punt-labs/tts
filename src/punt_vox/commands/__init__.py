"""Humble Object commands for the punt-vox CLI (python.md §Rule 5).

Each command is a ``@final`` callable class exported as a module-level
singleton -- ``model`` / ``provider`` / ``voice`` -- taking a
:class:`~punt_vox.commands._result.Ctx` plus command-specific arguments
and returning a :class:`~punt_vox.commands._result.CommandResult`
carrying rendered ``text``, machine-readable ``json_data``, an
``error`` flag, and an ``exit_code`` the CLI adapter in
:mod:`punt_vox.__main__` maps to a ``typer.Exit``. No I/O beyond what
the ``Ctx`` collaborators own.

Library callers can import and await these directly::

    from punt_vox.commands import Ctx, model
    ctx = Ctx(store=..., client=...)
    result = await model(ctx, name=None)
    print(result.text)
    # result.exit_code is 0 on success, 1 on an expected user error.
"""

from __future__ import annotations

from punt_vox.commands._result import CommandResult, Ctx
from punt_vox.commands.model import model
from punt_vox.commands.provider import provider
from punt_vox.commands.voice import voice

__all__ = [
    "CommandResult",
    "Ctx",
    "model",
    "provider",
    "voice",
]
