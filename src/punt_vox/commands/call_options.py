"""``vox call``'s Typer option aliases -- extracted so ``call.py`` stays under
the module-size threshold as the CLI's own surface (``--trace-turns`` for the
turn-timer trace, alongside the pre-existing ``--script``/``--session``) has
grown. Pure declarative ``Annotated`` aliases, no logic; mirrors how the
option aliases for other multi-verb CLIs in this package (``cli_rec.py``) sit
alongside their own commands rather than in a shared module -- these differ
only in that ``call.py`` had grown past the size where keeping them inline
was still readable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

__all__ = ["ScriptOpt", "SessionOpt", "TraceTurnsOpt", "TransferSessionOpt"]

ScriptOpt = Annotated[
    Path | None,
    typer.Option(
        "--script",
        help=(
            "Dev/test path: a JSON Lines file of scripted turns "
            '({"text": ..., "confidence": ...} per line), fed through '
            "synthetic audio instead of the microphone -- no hardware, no "
            "ElevenLabs credentials, no network for the human side of the "
            "turn. Omit for a real call: real microphone capture, "
            "transcribed by ElevenLabs. Either way, a daemon-side TTS "
            "provider must still be configured to speak the reply -- "
            "resolved the same way for both paths, before the call starts."
        ),
    ),
]
SessionOpt = Annotated[
    str | None,
    typer.Option(
        "--session", help="Attach to this session id instead of discovering one."
    ),
]
TransferSessionOpt = Annotated[
    str | None,
    typer.Option(
        "--session", help="Re-attach to this session id, or re-discover if omitted."
    ),
]
# --verbose/-v was rejected: vox already establishes that name/short form as
# the global "raise the client log level" flag (see __main__.py's Verbose
# alias, wired through OutputFlags). A second, same-named --verbose scoped
# to `call start` with a completely different, disjoint effect broke this
# CLI's own documented convention that flag POSITION never changes MEANING
# (OutputFlags's docstring: "vox --json status" and "vox status --json"
# select the same mode) -- "vox --verbose call start" and "vox call start
# --verbose" would otherwise silently do two different things with no error
# or hint either way. A distinct name sidesteps the collision outright
# rather than threading this feature through OutputFlags's own accumulation
# (which lives in __main__.py, an outer-layer module commands/ must not
# import from).
TraceTurnsOpt = Annotated[
    bool,
    typer.Option(
        "--trace-turns",
        help=(
            "Also print the turn-by-turn latency trace live to the terminal "
            "(speech detected, turn ended, STT sent/received, claude "
            "spawned, reply received, TTS sent/playback starts). Always "
            "recorded to vox.log at DEBUG regardless of this flag; this "
            "only adds the live terminal echo."
        ),
    ),
]
