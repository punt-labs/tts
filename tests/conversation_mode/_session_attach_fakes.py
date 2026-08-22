"""An in-memory ``SessionAttach`` fake for Conversation Mode tests.

Structural stand-in (no inheritance), matching ``tests/_program_fakes.py``'s
``FakeProgramGateway`` style: it records every call and lets a test control
what comes back. Unlike ``FakeProgramGateway``, what comes back is a
*sequence* of reply chunks with independently controllable timing between
them, not one scripted value -- a single-reply fake cannot exercise FR-11
(speaking begins on the reply's first complete portion, not the whole
reply), the exact behavior that makes a long agent turn feel alive
(``docs/conversation-mode-prd.tex`` UC-5).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Self, final

from punt_vox.voxd.conversation_mode.reply import ReplyChunk
from punt_vox.voxd.conversation_mode.session_attach import SessionAttachError
from punt_vox.voxd.conversation_mode.turn import TranscribedTurn


@final
@dataclass(frozen=True, slots=True)
class ScriptedChunk:
    """One reply chunk plus how long to wait before yielding it.

    ``delay_s`` is real elapsed time the fake awaits via ``asyncio.sleep``
    before yielding ``chunk`` -- not a wall-clock read, a caller-supplied
    parameter (the same discipline ``docs/conversation-mode-prd.tex``
    Chapter 3 requires of the turn/barge-in detectors), so a test controls
    the pacing between chunks explicitly rather than the fake inventing its
    own. Pass ``0.0`` for chunks whose relative ordering matters but whose
    absolute timing does not.
    """

    chunk: ReplyChunk
    delay_s: float = 0.0


@final
@dataclass(frozen=True, slots=True)
class SessionAttachCall:
    """One recorded call against the fake: the turn it was asked to forward."""

    turn: TranscribedTurn


@final
class FakeSessionAttach:
    """A stateful, subprocess-free ``SessionAttach`` for Conversation Mode tests."""

    __slots__ = ("_attach_error", "_calls", "_script")
    _script: tuple[ScriptedChunk, ...]
    # When set, ``send_turn`` raises this as a session-unreachable/session-ended
    # fault -- the path a real implementation hits when the agent session cannot
    # be resumed or ends a turn abnormally (SessionAttachError's contract).
    _attach_error: str | None
    _calls: list[SessionAttachCall]

    def __new__(
        cls,
        script: Sequence[ScriptedChunk] = (),
        *,
        attach_error: str | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._script = tuple(script)
        self._attach_error = attach_error
        self._calls = []
        return self

    async def send_turn(self, turn: TranscribedTurn) -> AsyncIterator[ReplyChunk]:
        self._calls.append(SessionAttachCall(turn=turn))
        if self._attach_error is not None:
            raise SessionAttachError(self._attach_error)
        for scripted in self._script:
            if scripted.delay_s:
                await asyncio.sleep(scripted.delay_s)
            yield scripted.chunk

    def calls(self) -> list[SessionAttachCall]:
        """Return the recorded calls in call order."""
        return list(self._calls)

    def turns(self) -> list[str]:
        """Return the recorded turn texts in call order (a test-readability helper)."""
        return [call.turn.text for call in self._calls]


__all__ = ["FakeSessionAttach", "ScriptedChunk", "SessionAttachCall"]
