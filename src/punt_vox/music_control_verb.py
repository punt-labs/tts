"""``ControlVerb`` -- a music control command and how it reports itself.

The seven verbs that drive the running Program (``on``, ``stop``, ``play``, and
the four transport steps) differ only in their name and the line to print when
the daemon acks without a reason of its own. Pairing the two here, with the
payload each builds, keeps that pair from being threaded through the surface as
two loose arguments and keeps a new verb to one constant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from punt_vox.types_programs.control import CommandOutcome

__all__ = [
    "ON",
    "PAUSE",
    "PLAY",
    "PREV",
    "RESUME",
    "STEP",
    "STOP",
    "ControlVerb",
]


@final
@dataclass(frozen=True, slots=True)
class ControlVerb:
    """One control command: its wire name and its daemon-is-silent fallback line."""

    name: str
    phrase: str

    def payload(self, outcome: CommandOutcome) -> tuple[dict[str, object], str]:
        """Return the JSON record and the human line reporting *outcome*.

        Both carry the same text: the daemon's own reason when it gave one, else
        this verb's phrase. A reject that explains itself in prose and says
        nothing in JSON is a failure a ``--json`` client cannot diagnose, so the
        reason goes in both channels or neither.
        """
        line = outcome.display(self.phrase)
        return {"music": self.name, "applied": outcome.applied, "message": line}, line


ON = ControlVerb("on", "Generating music.")
STOP = ControlVerb("stop", "Music stopped.")
PLAY = ControlVerb("play", "Playing selection.")
STEP = ControlVerb("next", "Advancing to another part.")
PREV = ControlVerb("prev", "Stepping to the previous part.")
PAUSE = ControlVerb("pause", "Paused.")
RESUME = ControlVerb("resume", "Resumed.")
