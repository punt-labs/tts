"""Per-turn latency diagnostics -- ``vox call start``'s turn-timer collaborator.

Two real latency sources were confirmed live: Option D spawns a fresh
``claude -p --resume`` subprocess per turn (28+ ``SessionStart`` hooks fire
before the first real assistant frame), and a long reply's real ElevenLabs
synthesis time can exceed the daemon client's own timeout. Neither is fixed
here -- this module exists only to make where time goes *visible*, either by
grepping ``vox.log`` after the fact or watching a call live with
``--trace-turns``. See :func:`punt_vox.logging_config.configure_turn_timer_logging`
for how the two are wired: every mark below is an unconditional
``logger.debug`` call, and that function decides where those records land.

:class:`TurnTimer` is a :data:`~typing.Protocol` (PY-DP-11: exactly one
method), matching the trigger-observer shape :class:`CallActor` already uses
for mode transitions -- except stages here are finer-grained than a mode
transition (STT request/response, subprocess spawn, first reply frame) and
carry an optional human-readable *detail* (a confidence score, an
approximation caveat), so a dedicated small Protocol fits better than
threading these through :class:`CallActor`'s existing transition-observer
list. :class:`LoggingTurnTimer` is the one production implementation; tests
inject their own :class:`TurnTimer`-shaped fake to assert on marks without
touching real logging configuration.
"""

from __future__ import annotations

import logging
import time
from typing import Literal, Protocol, Self, final, runtime_checkable

__all__ = ["LoggingTurnTimer", "TurnStage", "TurnTimer"]

logger = logging.getLogger(__name__)

# The closed set of stages a human watching a live call cares about, in the
# order a turn normally passes through them. ``claude_spawned`` and
# ``first_reply_frame`` are approximations from CallSession's vantage point
# -- it has no visibility into ClaudeSessionAttach's own subprocess spawn or
# stream-json frame boundaries (see call_session.py's marks for exactly what
# each approximates).
type TurnStage = Literal[
    "speech_first_detected",
    "turn_ended",
    "stt_request_sent",
    "stt_response_received",
    "ack_spoken",
    "claude_spawned",
    "first_reply_frame",
    "reply_complete",
    "tts_request_sent",
    "playback_started",
]


@runtime_checkable
class TurnTimer(Protocol):
    """Mark one turn's progress through its stages.

    ``mark(stage)`` for the first stage of a turn (``speech_first_detected``)
    starts that turn's own clock -- every subsequent mark until the next
    ``speech_first_detected`` reports elapsed time relative to it, alongside
    the time since the *previous* mark. No separate "start a turn" method:
    the first stage already only occurs once per turn, so overloading it as
    the clock reset avoids a second call every caller would have to
    remember.
    """

    def mark(self, stage: TurnStage, *, detail: str | None = None) -> None:
        """Record that *stage* was just reached, with an optional detail string."""
        ...


@final
class LoggingTurnTimer:
    """The one production :class:`TurnTimer`: logs one DEBUG line per stage.

    Always calls :meth:`logging.Logger.debug` -- no ``--verbose``/enabled
    check here. Whether that record reaches ``vox.log``, the terminal,
    both, or neither is entirely
    :func:`~punt_vox.logging_config.configure_turn_timer_logging`'s decision,
    made once by *this module's own logger's* level and handlers; this class
    has no opinion about it and no reference to the CLI flag.
    """

    __slots__ = ("_last_mark_at", "_turn_started_at")
    _turn_started_at: float
    """Reset to "now" on every `speech_first_detected` mark. Initialized at
    construction (not deferred to the first real mark) so this field is a
    plain ``float``, never an ``Optional`` a caller could observe -- a stage
    marked before any turn has begun (should not happen, but this is a
    diagnostic tool, not an enforcement point) simply reports elapsed time
    relative to construction instead of raising."""
    _last_mark_at: float
    """Mirrors :attr:`_turn_started_at` for the step-to-step delta."""

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        now = time.monotonic()
        self._turn_started_at = now
        self._last_mark_at = now
        return self

    def mark(self, stage: TurnStage, *, detail: str | None = None) -> None:
        """Log *stage* at DEBUG: elapsed since the previous mark, elapsed
        since this turn's `speech_first_detected`, and *detail* if given.

        ``speech_first_detected`` resets both clocks to zero for the new
        turn -- see the class docstring's "no separate start-turn method"
        rationale.
        """
        now = time.monotonic()
        if stage == "speech_first_detected":
            self._turn_started_at = now
            self._last_mark_at = now
        since_start = now - self._turn_started_at
        since_previous = now - self._last_mark_at
        self._last_mark_at = now
        logger.debug(self._render(stage, since_previous, since_start, detail))

    @staticmethod
    def _render(
        stage: TurnStage, since_previous: float, since_start: float, detail: str | None
    ) -> str:
        line = f"{stage} (+{since_previous:.2f}s step, +{since_start:.2f}s turn)"
        return f"{line} -- {detail}" if detail else line
