"""``vox call``'s Typer option aliases -- extracted so ``call.py`` stays under
the module-size threshold as the CLI's own surface (``--verbose`` for the
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

__all__ = ["ScriptOpt", "SessionOpt", "TransferSessionOpt", "VerboseOpt"]

ScriptOpt = Annotated[
    Path | None,
    typer.Option(
        "--script",
        help=(
            "Dev/test path: a JSON Lines file of scripted turns "
            '({"text": ..., "confidence": ...} per line), fed through '
            "synthetic audio instead of the microphone -- no hardware, no "
            "ElevenLabs credentials, no network. Omit for a real call: real "
            "microphone capture, transcribed by ElevenLabs."
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
VerboseOpt = Annotated[
    bool,
    typer.Option(
        "--verbose",
        "-v",
        help=(
            "Also print the turn-by-turn latency trace live to the terminal "
            "(speech detected, turn ended, STT sent/received, claude "
            "spawned, reply received, TTS sent/playback starts). Always "
            "recorded to vox.log at DEBUG regardless of this flag; this "
            "only adds the live terminal echo."
        ),
    ),
]
