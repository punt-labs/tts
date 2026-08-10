"""Pure async command functions for the punt-vox CLI.

Each function takes a :class:`~punt_vox.commands._result.Ctx` plus
command-specific arguments and returns a
:class:`~punt_vox.commands._result.CommandResult`. No I/O beyond what the
``Ctx`` collaborators own; no exit codes -- the CLI adapter in
:mod:`punt_vox.__main__` handles those.

Library callers can import and await these directly::

    from punt_vox.commands import Ctx, model
    ctx = Ctx(store=..., client=...)
    result = await model(ctx, name=None)
    print(result.text)
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
