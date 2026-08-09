"""The transport verbs of the ``music`` tool: ``next``, ``prev``, ``pause``, ``resume``.

Four commands that move the cursor of whatever is already playing. They read no
call arguments and touch neither the catalog nor the style register -- only the
running Program and the phrase to announce it with -- so they share no state
with the verbs that start, stop, or report a Program (PL-CO-2).

All four differ only in the daemon call and the marquee phrase, which is why
they collapse onto one render-and-report path: a new transport command is a
two-line method here, not another copy of the try/except/render shape.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Self, final

from punt_vox.music_faults import DAEMON_ERRORS, MusicFault

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.music_phrases import MusicMarquee
    from punt_vox.program_gateway import ProgramGateway
    from punt_vox.types_programs.control import CommandOutcome

__all__ = ["TransportVerbs"]


@final
class TransportVerbs:
    """Move the playing Program's cursor on behalf of the ``music`` tool.

    The gateway arrives as a factory called per invocation, so a caller that
    re-points the daemon connection between calls is honoured on the next one.
    The marquee is shared with the dispatcher rather than rebuilt, so the whole
    tool speaks with one DJ voice.
    """

    __slots__ = ("_gateway_factory", "_marquee")
    _gateway_factory: Callable[[], ProgramGateway]
    _marquee: MusicMarquee

    def __new__(
        cls, gateway_factory: Callable[[], ProgramGateway], marquee: MusicMarquee
    ) -> Self:
        self = super().__new__(cls)
        self._gateway_factory = gateway_factory
        self._marquee = marquee
        return self

    def advance(self) -> str:
        """Step the replay cursor forward, or skip the generating Program."""
        return self._run(self._gateway_factory().advance, self._marquee.skip())

    def prev(self) -> str:
        """Step the replay cursor back one part."""
        return self._run(self._gateway_factory().prev, "Previous part.")

    def pause(self) -> str:
        """Suspend the active source in place."""
        return self._run(self._gateway_factory().pause, "Paused.")

    def resume(self) -> str:
        """Continue a suspended source."""
        return self._run(self._gateway_factory().resume, "Resumed.")

    def _run(self, op: Callable[[], CommandOutcome], phrase: str) -> str:
        """Run transport command *op* and render its outcome.

        A daemon fault funnels to the same JSON error envelope every other
        ``music`` verb answers with, so a client parses one failure shape.
        """
        try:
            outcome = op()
        except DAEMON_ERRORS as exc:
            return MusicFault.of(exc)
        return json.dumps(
            {"message": f"♪ {outcome.display(phrase)}", "applied": outcome.applied}
        )
